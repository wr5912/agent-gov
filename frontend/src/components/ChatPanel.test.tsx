import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { chromium, type Browser, type Page } from "playwright";
import { renderToStaticMarkup } from "react-dom/server";
import {
  isPlaygroundRunLocked,
  playgroundRunReducer,
  type PlaygroundRunState,
} from "../playgroundRunState";
import { ChatPanel, PlaygroundErrorNotice } from "./ChatPanel";

let browser: Browser;
let page: Page;

beforeAll(async () => {
  browser = await chromium.launch({ headless: true });
  page = await browser.newPage();
});

afterAll(async () => {
  await page?.close();
  await browser?.close();
});

const activeRun: PlaygroundRunState = {
  phase: "running",
  operationId: "operation-1",
  runId: "run-1",
  sessionId: "session-1",
  source: "local",
};

function renderRunState(runState: PlaygroundRunState) {
  return renderToStaticMarkup(
    <ChatPanel
      messages={[]}
      input="执行当前任务"
      streaming={isPlaygroundRunLocked(runState)}
      runState={runState}
      activeSessionId="session-1"
      sessionSidebarOpen={false}
      agentName="业务 Agent"
      agentPresentation={null}
      runtimeReady
      onInputChange={() => undefined}
      onUsePromptSuggestion={() => undefined}
      onSend={() => undefined}
      onStop={() => undefined}
      onToggleSession={() => undefined}
      onOpenRuntimeSettings={() => undefined}
      onOpenFeedback={() => undefined}
      onOpenTrace={() => undefined}
      onGetContext={() => undefined}
      onRerun={() => undefined}
      userInputErrors={{}}
      submittingUserInputRequests={new Set()}
      onSubmitUserInput={() => undefined}
      onSubmitExternalExecution={() => undefined}
    />,
  );
}

describe("Playground 控制面错误的真实组件投影", () => {
  it("历史加载或鉴权失败以可访问的独立提示呈现并转义错误文本", () => {
    const html = renderToStaticMarkup(<PlaygroundErrorNotice error="加载历史会话失败：HTTP 403 <script>unsafe</script>" />);
    expect(html).toContain('data-testid="playground-error"');
    expect(html).toContain('role="alert"');
    expect(html).toContain("加载历史会话失败：HTTP 403");
    expect(html).toContain("&lt;script&gt;unsafe&lt;/script&gt;");
    expect(html).not.toContain("<script>");
  });

  it.each([undefined, ""])("恢复成功后的空错误 %s 不留下错误容器", (error) => {
    expect(renderToStaticMarkup(<PlaygroundErrorNotice error={error} />)).toBe("");
  });
});

describe("ChatPanel 真实 DOM 运行状态矩阵", () => {
  it.each([
    {
      name: "idle",
      state: { phase: "idle" } satisfies PlaygroundRunState,
      status: "Ready",
      stopLabel: undefined,
      stopDisabled: undefined,
    },
    {
      name: "running",
      state: activeRun,
      status: "运行中",
      stopLabel: "停止",
      stopDisabled: false,
    },
    {
      name: "reconciling",
      state: { ...activeRun, phase: "reconciling" } satisfies PlaygroundRunState,
      status: "状态待核对",
      stopLabel: "重试停止",
      stopDisabled: false,
    },
    {
      name: "cancelling",
      state: { ...activeRun, phase: "cancelling" } satisfies PlaygroundRunState,
      status: "停止中…",
      stopLabel: "停止中…",
      stopDisabled: true,
    },
    {
      name: "terminal",
      state: playgroundRunReducer(activeRun, {
        type: "terminal",
        operationId: "operation-1",
        outcome: "succeeded",
      }),
      status: "Ready",
      stopLabel: undefined,
      stopDisabled: undefined,
    },
  ])("$name 状态只渲染契约允许的主动作", async ({ state, status, stopLabel, stopDisabled }) => {
    await page.setContent(renderRunState(state));

    const statusNode = page.locator(".chat-header-actions .run-status, .chat-header-actions .idle-status");
    await expect(statusNode.textContent()).resolves.toContain(status);
    const stop = page.getByTestId("chat-stop");
    const send = page.getByTestId("chat-send");
    if (stopLabel === undefined) {
      await expect(stop.count()).resolves.toBe(0);
      await expect(send.count()).resolves.toBe(1);
      await expect(send.isDisabled()).resolves.toBe(false);
      return;
    }
    await expect(send.count()).resolves.toBe(0);
    await expect(stop.count()).resolves.toBe(1);
    await expect(stop.textContent()).resolves.toContain(stopLabel);
    await expect(stop.isDisabled()).resolves.toBe(stopDisabled);
  });
});
