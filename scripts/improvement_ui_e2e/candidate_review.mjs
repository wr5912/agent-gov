import { createHash } from "node:crypto";

import { apiJson } from "./runtime_client.mjs";

function sha256(value) {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function changedDiffEntries(diff) {
  return [
    ...(diff.added || []).map((entry) => ({ path: entry.path, status: "added" })),
    ...(diff.modified || []).map((entry) => ({ path: entry.path, status: "modified" })),
    ...(diff.deleted || []).map((entry) => ({ path: entry.path, status: "deleted" })),
  ];
}

function actualChangedLines(unifiedDiff) {
  return String(unifiedDiff || "").split("\n").filter((line) => (
    (line.startsWith("+") && !line.startsWith("+++"))
    || (line.startsWith("-") && !line.startsWith("---"))
  ));
}

async function reviewCandidateFiles(page, config, changeSet, candidateCommitSha) {
  const approveButton = page.getByTestId("release-action-approve");
  if (!await approveButton.isDisabled()) {
    throw new Error("candidate approval became enabled before the complete file Diff was displayed");
  }
  await page.getByTestId("release-action-view-changes").click();
  const summary = page.getByTestId("release-diff-summary");
  await summary.waitFor({ timeout: config.actionTimeoutMs });
  const encodedId = encodeURIComponent(changeSet.change_set_id);
  const diff = await apiJson(config, "/api/agent-change-sets/" + encodedId + "/diff");
  if (diff.from_version_id !== changeSet.base_commit_sha || diff.to_version_id !== candidateCommitSha) {
    throw new Error("reviewed candidate Diff lost its exact base/candidate identity: " + JSON.stringify(diff));
  }
  const entries = changedDiffEntries(diff);
  const reviewedFiles = [];
  if (!entries.length || new Set(entries.map((entry) => entry.path)).size !== entries.length) {
    throw new Error("candidate Diff has no unique reviewable file set: " + JSON.stringify(entries));
  }
  for (const entry of entries) {
    const query = new URLSearchParams({ path: entry.path });
    const detail = await apiJson(config, "/api/agent-change-sets/" + encodedId + "/file-diff?" + query);
    const changedLines = actualChangedLines(detail.unified_diff);
    if (detail.from_version_id !== diff.from_version_id
        || detail.to_version_id !== diff.to_version_id
        || detail.path !== entry.path
        || detail.status !== entry.status
        || detail.is_text !== true
        || detail.truncated !== false
        || !changedLines.length) {
      throw new Error("candidate file Diff is not completely reviewable: " + JSON.stringify(detail));
    }
    const fileSelector = await page.evaluate(
      (path) => `[data-testid="release-diff-file"][data-path="${CSS.escape(path)}"]`,
      entry.path,
    );
    const uiFile = summary.locator(fileSelector);
    await uiFile.waitFor({ timeout: config.actionTimeoutMs });
    const rendered = await uiFile.getByTestId("release-file-unified-diff").textContent();
    if (rendered !== detail.unified_diff) {
      throw new Error("UI did not render the complete exact Diff for " + entry.path);
    }
    reviewedFiles.push({ path: entry.path, detail_sha256: sha256(canonicalJson(detail)) });
  }
  if (await summary.getByTestId("release-diff-file").count() !== entries.length
      || await page.getByTestId("release-diff-error").count()
      || await summary.locator('input[type="checkbox"]').count()) {
    throw new Error("UI candidate Diff is incomplete or contains a fail-closed error");
  }
  await page.getByTestId("release-approval-confirmation-note")
    .filter({ hasText: `已审阅 ${entries.length} 个完整文件 Diff` })
    .waitFor({ timeout: config.actionTimeoutMs });
  return { diffDigest: sha256(canonicalJson(diff)), reviewedFileCount: entries.length, reviewedFiles };
}

export async function reviewAndApprovePassedCandidate(page, config, evidence) {
  const encodedId = encodeURIComponent(evidence.changeSetId);
  const changeSet = await apiJson(config, "/api/agent-change-sets/" + encodedId);
  if (changeSet.status !== "pending_approval"
      || changeSet.candidate_commit_sha !== evidence.candidateCommitSha) {
    throw new Error("sensitive candidate is not awaiting approval at the exact tested commit: " + JSON.stringify(changeSet));
  }
  const review = await reviewCandidateFiles(page, config, changeSet, evidence.candidateCommitSha);
  const endpoint = "/api/agent-change-sets/" + encodedId + "/approve";
  const button = page.getByTestId("release-action-approve");
  await page.waitForFunction(() => {
    const candidate = document.querySelector('[data-testid="release-action-approve"]');
    return candidate instanceof HTMLButtonElement && !candidate.disabled;
  }, null, { timeout: config.actionTimeoutMs });
  const responsePromise = page.waitForResponse((response) => (
    response.request().method() === "POST" && new URL(response.url()).pathname === endpoint
  ), { timeout: config.actionTimeoutMs });
  void responsePromise.catch(() => undefined);
  const approvalDialogs = [];
  const publicationRequests = [];
  const onDialog = (dialog) => {
    approvalDialogs.push(dialog.type());
    void dialog.dismiss();
  };
  const onRequest = (request) => {
    if (request.method() === "POST" && new URL(request.url()).pathname === endpoint.replace(/\/approve$/, "/publish")) {
      publicationRequests.push(request.url());
    }
  };
  page.on("dialog", onDialog);
  page.on("request", onRequest);
  try {
    await button.click();
    if (approvalDialogs.length) throw new Error("candidate approval unexpectedly opened a second confirmation dialog");
    const response = await responsePromise;
    if (!response.ok()) throw new Error("candidate approval failed: " + response.status() + " " + await response.text());
    const requestBody = response.request().postDataJSON();
    if (requestBody?.candidate_commit_sha !== evidence.candidateCommitSha
        || requestBody?.diff_digest !== review.diffDigest
        || requestBody?.test_run_id !== evidence.testRunId
        || requestBody?.suite_digest !== evidence.suiteDigest
        || canonicalJson(requestBody?.reviewed_files) !== canonicalJson(review.reviewedFiles)) {
      throw new Error("approval request did not carry the exact reviewed candidate/Diff/test/file evidence");
    }
    const approved = await response.json();
    const approval = approved.approval_evidence || {};
    if (approved.status !== "approved"
        || approval.candidate_commit_sha !== evidence.candidateCommitSha
        || approval.test_run_id !== evidence.testRunId
        || approval.suite_digest !== evidence.suiteDigest
        || approval.diff_digest !== review.diffDigest
        || approval.reviewed_file_count !== review.reviewedFileCount
        || typeof approval.review_digest !== "string") {
      throw new Error("approval evidence lost exact candidate/Diff/test binding: " + JSON.stringify(approved));
    }
    await page.getByTestId("release-approval-evidence").filter({ hasText: approval.test_run_id }).waitFor({ timeout: config.actionTimeoutMs });
    if (publicationRequests.length) throw new Error("candidate approval unexpectedly published the candidate");
    return {
      approved,
      approvalEndpoint: endpoint,
      approvalStatus: response.status(),
      ...review,
    };
  } finally {
    page.off("dialog", onDialog);
    page.off("request", onRequest);
  }
}
