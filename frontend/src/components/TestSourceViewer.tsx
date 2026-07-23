import { python } from "@codemirror/lang-python";
import { EditorView, type ViewUpdate } from "@codemirror/view";
import CodeMirror, { type ReactCodeMirrorRef } from "@uiw/react-codemirror";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import type { AgentTestSuiteFile } from "../types/runtime";

type TestFileSymbol = NonNullable<AgentTestSuiteFile["symbols"]>[number];

function symbolKey(symbol: TestFileSymbol) {
  return `${symbol.kind}:${symbol.qualified_name}:${symbol.line}`;
}

function symbolKindLabel(kind: TestFileSymbol["kind"]) {
  if (kind === "class") return "class";
  return kind === "async_function" ? "async test" : "test";
}

function symbolPosition(line: number, lineCount: number) {
  if (lineCount <= 1) return 2;
  const progress = (Math.min(lineCount, Math.max(1, line)) - 1) / (lineCount - 1);
  return 2 + (progress * 96);
}

function activeSymbolInViewport(symbols: TestFileSymbol[], firstLine: number, lastLine: number) {
  const centerLine = (firstLine + lastLine) / 2;
  const visible = symbols.filter((symbol) => symbol.line >= firstLine && symbol.line <= lastLine);
  if (visible.length) {
    return visible.reduce((nearest, symbol) => (
      Math.abs(symbol.line - centerLine) < Math.abs(nearest.line - centerLine) ? symbol : nearest
    ));
  }
  return symbols.filter((symbol) => symbol.line < firstLine).at(-1) ?? symbols[0];
}

function activeSymbolForView(symbols: TestFileSymbol[], view: EditorView) {
  const firstPosition = view.visibleRanges[0]?.from ?? view.viewport.from;
  const lastPosition = view.visibleRanges.at(-1)?.to ?? view.viewport.to;
  return activeSymbolInViewport(
    symbols,
    view.state.doc.lineAt(firstPosition).number,
    view.state.doc.lineAt(lastPosition).number,
  );
}

function cancelAnimationFrameRef(frameRef: { current: number | undefined }) {
  if (frameRef.current === undefined) return;
  window.cancelAnimationFrame(frameRef.current);
  frameRef.current = undefined;
}

function TestSourceSymbolRail({
  symbols,
  lineCount,
  activeKey,
  onJump,
}: {
  symbols: TestFileSymbol[];
  lineCount: number;
  activeKey?: string;
  onJump: (symbol: TestFileSymbol) => void;
}) {
  const [hoveredKey, setHoveredKey] = useState<string>();
  const [focusedKey, setFocusedKey] = useState<string>();
  const previewKey = focusedKey ?? hoveredKey;
  const preview = symbols.find((symbol) => symbolKey(symbol) === previewKey);

  return (
    <nav className="test-source-symbol-rail" data-testid="test-source-symbol-rail" aria-label="测试源码符号导航">
      <div className="test-source-symbol-marks">
        {symbols.map((symbol) => {
          const key = symbolKey(symbol);
          const label = `${symbolKindLabel(symbol.kind)} ${symbol.qualified_name}，第 ${symbol.line} 行`;
          return (
            <button
              aria-current={key === activeKey ? "location" : undefined}
              aria-label={`定位到 ${label}`}
              className={`test-source-symbol-mark ${key === activeKey ? "is-active" : ""}`}
              data-symbol-line={symbol.line}
              data-testid="test-source-symbol-mark"
              key={key}
              style={{ "--test-symbol-position": `${symbolPosition(symbol.line, lineCount)}%` } as CSSProperties}
              type="button"
              onBlur={() => setFocusedKey(undefined)}
              onClick={() => onJump(symbol)}
              onFocus={() => setFocusedKey(key)}
              onPointerEnter={() => setHoveredKey(key)}
              onPointerLeave={() => setHoveredKey(undefined)}
            >
              <span aria-hidden="true" />
            </button>
          );
        })}
      </div>
      {preview ? (
        <div
          className="test-source-symbol-preview"
          data-testid="test-source-symbol-preview"
          style={{ "--test-symbol-position": `${symbolPosition(preview.line, lineCount)}%` } as CSSProperties}
        >
          <span>{symbolKindLabel(preview.kind)}</span>
          <strong>{preview.qualified_name}</strong>
          <code>L{preview.line}</code>
        </div>
      ) : null}
    </nav>
  );
}

function TestSourceHeader({
  sourceFile,
  testFiles,
  onSelectFile,
  onCopySource,
}: {
  sourceFile: AgentTestSuiteFile;
  testFiles: string[];
  onSelectFile: (path: string) => void;
  onCopySource: () => void;
}) {
  return (
    <div className="test-source-head">
      <label className="test-file-picker">
        <span>测试文件</span>
        <select className="iw-select" data-testid="test-file-select" value={sourceFile.path} onChange={(event) => onSelectFile(event.target.value)}>
          {testFiles.map((path) => <option key={path} value={path}>{path}</option>)}
        </select>
      </label>
      <div className="test-source-actions">
        <span>{sourceFile.line_count} 行</span>
        <button className="iw-secondary-button" type="button" onClick={onCopySource}>复制源码</button>
      </div>
    </div>
  );
}

export function TestSourceViewer({
  sourceFile,
  testFiles,
  onSelectFile,
  onCopySource,
}: {
  sourceFile: AgentTestSuiteFile;
  testFiles: string[];
  onSelectFile: (path: string) => void;
  onCopySource: () => void;
}) {
  const editorRef = useRef<ReactCodeMirrorRef>(null);
  const jumpTargetRef = useRef<string | undefined>(undefined);
  const jumpUnlockFrameRef = useRef<number | undefined>(undefined);
  const symbols = useMemo(() => sourceFile.symbols ?? [], [sourceFile.symbols]);
  const [activeKey, setActiveKey] = useState<string>();

  useEffect(() => {
    cancelAnimationFrameRef(jumpUnlockFrameRef);
    jumpTargetRef.current = undefined;
    setActiveKey(symbols[0] ? symbolKey(symbols[0]) : undefined);
  }, [sourceFile.commit_sha, sourceFile.path, symbols]);

  useEffect(() => () => cancelAnimationFrameRef(jumpUnlockFrameRef), []);

  const handleEditorUpdate = useCallback((update: ViewUpdate) => {
    if ((!update.viewportChanged && !update.geometryChanged) || jumpTargetRef.current) return;
    const active = activeSymbolForView(symbols, update.view);
    const nextKey = active ? symbolKey(active) : undefined;
    setActiveKey((current) => current === nextKey ? current : nextKey);
  }, [symbols]);

  const jumpToSymbol = useCallback((symbol: TestFileSymbol) => {
    const view = editorRef.current?.view;
    if (!view) return;
    const key = symbolKey(symbol);
    const target = view.state.doc.line(Math.min(view.state.doc.lines, Math.max(1, symbol.line)));
    cancelAnimationFrameRef(jumpUnlockFrameRef);
    jumpTargetRef.current = key;
    view.dispatch({
      selection: { anchor: target.from },
      effects: EditorView.scrollIntoView(target.from, { y: "center" }),
    });
    setActiveKey(key);
    jumpUnlockFrameRef.current = window.requestAnimationFrame(() => {
      jumpUnlockFrameRef.current = window.requestAnimationFrame(() => {
        jumpUnlockFrameRef.current = undefined;
        jumpTargetRef.current = undefined;
        const active = editorRef.current?.view ? activeSymbolForView(symbols, editorRef.current.view) : undefined;
        setActiveKey(active ? symbolKey(active) : undefined);
      });
    });
  }, [symbols]);

  return (
    <div className="test-source-panel">
      <TestSourceHeader sourceFile={sourceFile} testFiles={testFiles} onSelectFile={onSelectFile} onCopySource={onCopySource} />
      <div className="test-source-code" data-testid="test-source-code">
        <CodeMirror
          basicSetup={{ lineNumbers: true, foldGutter: true, highlightSelectionMatches: true }}
          editable={false}
          extensions={[python(), EditorView.lineWrapping]}
          height="100%"
          key={`${sourceFile.commit_sha}:${sourceFile.path}`}
          readOnly
          ref={editorRef}
          value={sourceFile.content}
          onUpdate={handleEditorUpdate}
        />
        {symbols.length ? (
          <TestSourceSymbolRail symbols={symbols} lineCount={sourceFile.line_count} activeKey={activeKey} onJump={jumpToSymbol} />
        ) : null}
      </div>
    </div>
  );
}
