export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

export interface Session {
  id: string;
  title: string;
  web_search_enabled: boolean;
  last_message_at: string;
  created_at: string;
  updated_at: string;
}

export interface Message {
  id: string;
  role: string;
  kind: string;
  payload: Record<string, JsonValue>;
  sequence_no: number;
  created_at: string;
}

export interface Resource {
  id: string;
  title: string;
  source_type: string;
  created_at?: string;
}

export interface ResourceVersion {
  id: string;
  version_number: number;
  content: string;
  source: string;
  created_at: string;
}

export interface Citation {
  citation_id: string;
  resource_id: string;
  section_title: string;
  snippet: string;
  section_id?: string;
  section_type?: string;
  window?: { group_id?: string; start_order?: number; end_order?: number };
}

export type RunStatus = "queued" | "running" | "waiting_input" | "waiting_approval" | "succeeded" | "failed" | "cancelled";

export interface RunSummary {
  id: string;
  workspace_id: string;
  resource_id?: string;
  session_id?: string;
  request_id?: string;
  current_step?: string;
  pending_approval_id?: string;
  status: RunStatus;
  objective: string;
  step_count: number;
  completed_step_count: number;
  failed_step_count: number;
  created_at: string;
  updated_at: string;
}

export interface PublicRun {
  id: string;
  resource_id?: string;
  session_id?: string;
  request_id?: string;
  current_step?: string;
  deadline_at?: string;
  cancel_requested_at?: string;
  status: RunStatus;
  objective: string;
  created_at: string;
  updated_at: string;
}

export interface RunStep {
  id: string;
  step_key: string;
  step_type: string;
  status: string;
  attempt_count: number;
  max_attempts: number;
  next_retry_at?: string;
  created_at: string;
  updated_at: string;
}

export interface ToolCall {
  id: string;
  step_id: string;
  tool_name: string;
  tool_version: string;
  status: string;
  error_category?: string;
  started_at?: string;
  completed_at?: string;
}

export interface Finding {
  severity: string;
  code: string;
  message: string;
}

export interface ApprovalView {
  id: string;
  run_id: string;
  step_id: string;
  tool_name: string;
  status: string;
  created_at: string;
  decided_at?: string;
}

export interface Approval {
  id: string;
  workspace_id: string;
  run_id: string;
  step_id: string;
  resource_id?: string;
  session_id?: string;
  objective: string;
  tool_name: string;
  tool_version: string;
  reason: string;
  status: "pending" | "approved" | "rejected" | "cancelled";
  resources: JsonValue;
  payload: JsonValue;
  created_at: string;
  decision_reason?: string;
  decided_at?: string;
}

export interface RunDetail {
  run: PublicRun;
  steps: RunStep[];
  tool_calls: ToolCall[];
  approvals: ApprovalView[];
  findings: Finding[];
}

export interface Conversation {
  session: Session;
  messages: Message[];
}

export interface UploadResult extends Conversation {
  resource: Resource | null;
  error_message: string | null;
}

export interface UploadCapabilities {
  supported_extensions: string[];
  accept: string;
  hint: string;
}

export type SSEEventName = "session_created" | "session_file" | "message_started" | "message_delta" | "message_completed" | "task_suggestion" | "turn_state" | "error" | "done" | string;

export interface SSEFrame {
  id: number;
  event: SSEEventName;
  data: Record<string, JsonValue>;
}
