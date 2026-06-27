import type { ReactNode } from "react";
import { DrawerShell, type DrawerSize } from "./DrawerShell";

// v2.7 W3 修订：四阶段面板「查看详情」统一详情抽屉（复用 DrawerShell）。
// 每个只读卡片头部「查看详情」点击 → openDetail(StageDetail) → 在此渲染对应内容，
// 抽屉内容与卡片语义一一对应（不再共用通用 context 抽屉）。
export interface StageDetail {
  key: string;
  title: string;
  description?: string;
  size?: DrawerSize;
  headerActions?: ReactNode;
  content: ReactNode;
}

export function StageDetailDrawer({ detail, onClose }: { detail: StageDetail; onClose: () => void }) {
  return (
    <DrawerShell
      title={detail.title}
      description={detail.description}
      size={detail.size ?? "medium"}
      testId="stage-detail-drawer"
      dataState={detail.key}
      headerActions={detail.headerActions}
      onClose={onClose}
    >
      <div data-testid="stage-detail-content" data-detail-key={detail.key}>
        {detail.content}
      </div>
    </DrawerShell>
  );
}
