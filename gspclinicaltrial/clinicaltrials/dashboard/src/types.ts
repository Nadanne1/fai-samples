/** Shared type definitions for the Clinical Trial Screening Dashboard. */

export interface Patient {
  id: string;
  name: string;
  gender: string;
  birthDate: string;
  inQueue: boolean;
  trialId: string | null;
  priority: string | null;
}

export interface QueueMessage {
  messageId: string;
  patientId: string;
  trialId: string;
  priority: string;
  queuedAt: string;
  siteId: string;
}

export interface Trial {
  trialId: string;
  version?: string;
  title: string;
  phase: string;
  status: string;
  conditions: string[];
  questionnaireId?: string;
}

export interface ChatMessage {
  role: "assistant" | "user" | "system";
  content: string;
}

export interface QuestionnaireItem {
  linkId: string;
  text: string;
  type: string;
  required: boolean;
  category: string;
  source?: string;
  editable?: boolean;
  enableWhen?: Record<string, unknown>[];
  _excluded?: boolean;
}

export interface ExtractedTrial {
  trial_id?: string;
  title: string;
  phase: string;
  conditions: string[];
  interventions: string[];
  age_min?: number;
  age_max?: number;
  inclusion_criteria: string[];
  exclusion_criteria: string[];
  blinded?: boolean;
  monitoring_frequency?: string;
}

export interface BuilderResponse {
  session_id: string;
  messages: ChatMessage[];
  status: "gathering" | "review" | "created";
  extracted?: ExtractedTrial;
  questionnaire_items?: QuestionnaireItem[];
  standard_items?: QuestionnaireItem[];
  generated_items?: QuestionnaireItem[];
  error?: string;
}

export interface CreateTrialResponse {
  trial_id: string;
  questionnaire_id: string;
  questionnaire_items: number;
  status: string;
  title: string;
  phase: string;
  error?: string;
}

export interface ScreeningSession {
  session_id: string;
  messages: ChatMessage[];
  status: string;
  questionnaire_items?: number;
  progress?: { answered: number; total: number };
  eligibility?: {
    determination: string;
    criteria_results: { linkId: string; text: string; answer: unknown; result: string }[];
  };
  discrepancy?: { severity: string; detail: string };
  discrepancies?: { severity: string; detail: string }[];
  agentValidation?: {
    responseValid: boolean;
    responseIssue: string | null;
    extractedAnswerCorrect: boolean;
    nextQuestionRelevant: boolean;
    nextQuestionRepeated: boolean;
    suggestedAction: string;
    confidence: number;
    skipped?: boolean;
  };
}

export interface AnalyticsData {
  total_screenings: number;
  unique_patients: number;
  by_determination: Record<string, number>;
  by_trial: Record<string, { total: number; eligible: number; ineligible: number; borderline?: number; title?: string }>;
  recent_screenings: {
    id: string;
    patient_id: string;
    trial_id: string;
    determination: string;
    authored: string;
    session_id: string;
  }[];
  audit_records: number;
  queue_depth: number;
}

export interface PatientDetail {
  patient: Record<string, unknown>;
  conditions: Record<string, unknown>[];
  medications: Record<string, unknown>[];
  observations: Record<string, unknown>[];
  allergies: Record<string, unknown>[];
  procedures: Record<string, unknown>[];
}

export interface TrialDetail {
  trial_id: string;
  title: string;
  phase: string;
  status: string;
  conditions: string[];
  interventions: string[];
  blinding: Record<string, unknown>;
  questionnaire_id: string;
  questionnaire_items: number;
  eligibility_rules: {
    id: string;
    type: string;
    description: string;
    data_type: string;
    fhir_path?: string;
  }[];
  total_screenings: number;
  by_determination: Record<string, number>;
  top_failure_criteria: [string, number][];
  screenings: {
    id: string;
    patient_id: string;
    authored: string;
    determination: string;
    session_id: string;
    item_count: number;
    criteria_results: { linkId: string; text: string; result: string }[];
  }[];
  terminology_versions: Record<string, string>;
  retention_years: number;
  created_at: string;
}

export type ViewName = "screening" | "analytics" | "builder" | "flow-editor" | "devops" | "users";
