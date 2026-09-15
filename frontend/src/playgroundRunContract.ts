import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import type { DetachedRunRefs, PlaygroundActiveTurn } from "./playgroundDetachedRun";
import type { PlaygroundRunAction, PlaygroundRunState } from "./playgroundRunState";
import type {
  ChatMessage,
  RuntimeClientConfig,
  RuntimeExternalExecutionRequest,
  RuntimeUserConfirmRequest,
} from "./types/runtime";

export type MessageUpdater = (messages: ChatMessage[]) => ChatMessage[];
export type AssistantUpdater = (message: ChatMessage) => ChatMessage;

interface PromptSuggestionController {
  clear: (sessionId: string | undefined) => void;
}

export interface PlaygroundRunOptions {
  clientConfig: RuntimeClientConfig;
  input: string;
  runState: PlaygroundRunState;
  dispatchRun: Dispatch<PlaygroundRunAction>;
  activeSessionId: string | undefined;
  activeMessages: ChatMessage[];
  activeMessagesLoaded: boolean;
  selectedBusinessAgentId: string;
  runtimeAgentId: string;
  promptSuggestion: PromptSuggestionController;
  setInput: Dispatch<SetStateAction<string>>;
  setStreamingAssistantMessageId: Dispatch<SetStateAction<string | undefined>>;
  setLastError: Dispatch<SetStateAction<string | undefined>>;
  setSessionSidebarOpen: Dispatch<SetStateAction<boolean>>;
  setEvidencePanelOpen: Dispatch<SetStateAction<boolean>>;
  setActiveTraceMessageId: Dispatch<SetStateAction<string | undefined>>;
  setUserInputErrors: Dispatch<SetStateAction<Record<string, string>>>;
  setSubmittingUserInputRequests: Dispatch<SetStateAction<Set<string>>>;
  claimLocalSession: (sessionId: string, businessAgentId: string, runtimeAgentId: string) => void;
  updateSessionMessages: (sessionId: string, updater: MessageUpdater) => void;
  updateUserConfirmRequest: (requestId: string, patch: Partial<RuntimeUserConfirmRequest>) => void;
  updateExternalExecutionRequest: (requestId: string, patch: Partial<RuntimeExternalExecutionRequest>) => void;
  cancelUserConfirmForMessage: (sessionId: string, messageId: string) => void;
  cancelExternalExecutionForMessage: (sessionId: string, messageId: string) => void;
  calibrateTrace: (sessionId: string, messageId: string, runId: string) => Promise<void>;
  refresh: () => Promise<void>;
}

export interface RunRefs extends DetachedRunRefs {
  creatingSession: MutableRefObject<boolean>;
  continuationSubmissions: MutableRefObject<Set<string>>;
  sessionCreationIntent: MutableRefObject<{ agentId: string; key: string } | null>;
  detachedStop: MutableRefObject<Promise<void> | null>;
  detachedStopController: MutableRefObject<AbortController | null>;
}

export type ActiveTurn = PlaygroundActiveTurn;
