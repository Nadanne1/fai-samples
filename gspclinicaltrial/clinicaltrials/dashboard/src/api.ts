/** API client for the Clinical Trial Screening backend. */

import type {
  AnalyticsData,
  BuilderResponse,
  CreateTrialResponse,
  PatientDetail,
  ScreeningSession,
  Trial,
  TrialDetail,
} from "./types";

const API = import.meta.env.VITE_API_URL || "";
const API_KEY = import.meta.env.VITE_API_KEY || "";

let _accessToken = "";

/** Call once after sign-in (and after token refresh) so all requests carry the Bearer token. */
export function setAccessToken(token: string) {
  _accessToken = token;
}

async function request<T>(path: string, options?: RequestInit, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(API_KEY ? { "X-Api-Key": API_KEY } : {}),
      ...(_accessToken ? { Authorization: `Bearer ${_accessToken}` } : {}),
      ...options?.headers,
    },
    signal,
  });
  if (!res.ok) throw new Error(`API error: ${res.status} ${res.statusText}`);
  return res.json();
}

// Patients
export const fetchPatients = (count = 50) =>
  request<{ patients: import("./types").Patient[]; total: number }>(`/api/patients?count=${count}`);

// Queue
export const fetchQueue = () =>
  request<{ depth: number; inFlight: number; messages: import("./types").QueueMessage[] }>("/api/queue");

// Trials
export const fetchTrials = () =>
  request<{ trials: Trial[] }>("/api/trials");

export const fetchTrialDetail = (trialId: string, signal?: AbortSignal) =>
  request<TrialDetail>(`/api/trial/${trialId}`, undefined, signal);

export const deleteTrial = (trialId: string) =>
  request<{ status: string; trial_id: string }>(`/api/trial/${trialId}`, { method: "DELETE" });

// Patient detail
export const fetchPatientDetail = (patientId: string, signal?: AbortSignal) =>
  request<PatientDetail>(`/api/patient/${patientId}`, undefined, signal);

export const fetchPatientInsights = (patientId: string, signal?: AbortSignal) =>
  request<{ insights: Record<string, unknown> | null; generated_at: string | null }>(
    `/api/patient/${patientId}/insights`,
    undefined,
    signal
  );

export const fetchPatientTrialHistory = (patientId: string) =>
  request<{ history: Record<string, unknown>[]; total: number }>(`/api/patient/${patientId}/trial-history`);

// Screening chat
export const startScreeningChat = (patientId: string, trialId: string) =>
  request<ScreeningSession>("/api/chat/start", {
    method: "POST",
    body: JSON.stringify({ patient_id: patientId, trial_id: trialId }),
  });

export const sendScreeningResponse = (sessionId: string, message: string) =>
  request<ScreeningSession>("/api/chat/respond", {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId, message }),
  });

// Analytics
export const fetchAnalytics = () =>
  request<AnalyticsData>("/api/analytics");

// Trial Builder
export const startBuilderSession = () =>
  request<BuilderResponse>("/api/builder/start", { method: "POST" });

export const sendBuilderMessage = (sessionId: string, message: string) =>
  request<BuilderResponse>("/api/builder/respond", {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId, message }),
  });

export const createTrial = (
  sessionId: string,
  approvedItems: import("./types").QuestionnaireItem[],
  trialOverrides?: Record<string, unknown>
) =>
  request<CreateTrialResponse>("/api/builder/create", {
    method: "POST",
    body: JSON.stringify({
      session_id: sessionId,
      approved_items: approvedItems,
      trial_overrides: trialOverrides,
    }),
  });

// ClinicalTrials.gov Search & Import
export interface CtgStudy {
  nctId: string;
  briefTitle: string;
  officialTitle: string;
  overallStatus: string;
  phase: string;
  conditions: string[];
  interventions: string[];
  sponsor: string;
  enrollment: number | null;
  startDate: string;
}

export interface CtgSearchResult {
  studies: CtgStudy[];
  totalCount: number;
  nextPageToken: string;
  error?: string;
}

export interface CtgStudyDetail {
  nctId: string;
  briefTitle: string;
  officialTitle: string;
  overallStatus: string;
  phase: string;
  conditions: string[];
  interventions: string[];
  eligibilityCriteria: string;
  sex: string;
  minimumAge: string;
  maximumAge: string;
  briefSummary: string;
  sponsor: string;
  enrollment: number | null;
  error?: string;
}

export interface CtgImportResult {
  status: string;
  nctId: string;
  title: string;
  phase: string;
  conditions: string[];
  interventions: string[];
  builderSessionId: string;
  builderStatus: string;
  extracted?: import("./types").ExtractedTrial;
  questionnaireItems?: import("./types").QuestionnaireItem[];
  error?: string;
}

export const searchCtg = (params: {
  query?: string;
  condition?: string;
  status?: string;
  phase?: string;
  pageSize?: number;
  pageToken?: string;
}) => {
  const qs = new URLSearchParams();
  if (params.query) qs.set("query", params.query);
  if (params.condition) qs.set("condition", params.condition);
  if (params.status) qs.set("status", params.status);
  if (params.phase) qs.set("phase", params.phase);
  if (params.pageSize) qs.set("page_size", String(params.pageSize));
  if (params.pageToken) qs.set("page_token", params.pageToken);
  return request<CtgSearchResult>(`/api/ctg/search?${qs.toString()}`);
};

export const fetchCtgStudy = (nctId: string, signal?: AbortSignal) =>
  request<CtgStudyDetail>(`/api/ctg/study/${nctId}`, undefined, signal);

export const importCtgTrial = (nctId: string) =>
  request<CtgImportResult>("/api/ctg/import", {
    method: "POST",
    body: JSON.stringify({ nct_id: nctId }),
  });

// HealthLake Mapping
export interface HealthLakeMappingResource {
  resourceType: string;
  description: string;
  fhirId?: string;
  status: string;
  fields?: { field: string; value: string; path: string }[];
  extensions?: { name: string; url: string; hasData: boolean }[];
  items?: { linkId: string; text: string; type: string; required: boolean; hasEnableWhen: boolean; hasCodes: boolean }[];
  fhirPaths?: Record<string, { ruleId: string; description: string; type: string; fhirPath: string; dataType: string }[]>;
  totalRules?: number;
  versions?: Record<string, string>;
}

export interface HealthLakeMapping {
  trialId: string;
  title: string;
  resources: HealthLakeMappingResource[];
}

export const fetchHealthLakeMapping = (trialId: string, signal?: AbortSignal) =>
  request<HealthLakeMapping>(`/api/trial/${trialId}/healthlake-mapping`, undefined, signal);


// Screening Rules
export interface ScreeningRule {
  id: string;
  name: string;
  description: string;
  trigger: string;
  triggerConfig: Record<string, unknown>;
  action: string;
  actionConfig: Record<string, unknown>;
  enabled: boolean;
  priority: number;
  category: string;
}

export const fetchScreeningRules = () =>
  request<{ rules: ScreeningRule[] }>("/api/screening-rules");

export const updateScreeningRule = (ruleId: string, updates: Partial<ScreeningRule>) =>
  request<ScreeningRule>(`/api/screening-rules/${ruleId}`, {
    method: "PUT",
    body: JSON.stringify(updates),
  });

export const createScreeningRule = (rule: Omit<ScreeningRule, "id">) =>
  request<ScreeningRule>("/api/screening-rules", {
    method: "POST",
    body: JSON.stringify(rule),
  });

export const deleteScreeningRule = (ruleId: string) =>
  request<{ status: string }>(`/api/screening-rules/${ruleId}`, { method: "DELETE" });

export const resetScreeningRules = () =>
  request<{ rules: ScreeningRule[] }>("/api/screening-rules/reset", { method: "POST" });

export interface DemoResetSummary {
  screening_history_deleted: number;
  devops_tests_deleted: number;
  trials_deleted: number;
  questionnaires_deleted: number;
  questionnaire_responses_deleted: number;
  research_studies_deleted: number;
  screening_rules_restored: number;
  queues_purged: string[];
  assistant_sessions_cleared: number;
  preserved_trials: string[];
  warnings: string[];
}

export const resetDemoData = () =>
  request<{ status: string; summary: DemoResetSummary }>("/api/admin/demo/reset", {
    method: "POST",
    body: JSON.stringify({ confirm: "RESET_DEMO" }),
  });
