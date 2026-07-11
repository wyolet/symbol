use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct CorpusManifest {
    pub version: u64,
    pub corpora: Vec<Corpus>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Corpus {
    pub id: String,
    pub url: String,
    pub language: String,
    #[serde(default)]
    pub revision: Option<String>,
    #[serde(default)]
    pub source_paths: Vec<String>,
    #[serde(default)]
    pub exclude_paths: Vec<String>,
    #[serde(default = "default_history_limit")]
    pub history_limit: usize,
    #[serde(default = "default_max_files")]
    pub max_files: usize,
    #[serde(default = "default_max_diff_lines")]
    pub max_diff_lines: usize,
    #[serde(default)]
    pub check_command: Vec<String>,
}

fn default_history_limit() -> usize {
    1_000
}

fn default_max_files() -> usize {
    6
}

fn default_max_diff_lines() -> usize {
    400
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Candidate {
    pub version: u64,
    pub id: String,
    pub corpus: String,
    pub language: String,
    pub parent: String,
    pub commit: String,
    pub summary: String,
    pub files: Vec<FileDelta>,
    #[serde(default)]
    pub module_context: BTreeMap<String, String>,
    pub added_lines: usize,
    pub deleted_lines: usize,
    pub diff: String,
    pub declarations: Vec<DeclarationDelta>,
    pub operation_hints: Vec<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct FileDelta {
    pub path: String,
    pub added: usize,
    pub deleted: usize,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct DeclarationDelta {
    pub sign: String,
    pub kind: String,
    pub name: String,
    pub line: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DecisionKind {
    Accept,
    Reject,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ModelDecision {
    pub decision: DecisionKind,
    pub reason: String,
    #[serde(default)]
    pub task: Option<TaskSpec>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct TaskSpec {
    pub title: String,
    pub objective: String,
    pub primary_op: String,
    pub args: BTreeMap<String, Value>,
    #[serde(default)]
    pub pre_reads: Vec<String>,
    pub expected_files: Vec<String>,
    #[serde(default)]
    pub coverage_tags: Vec<String>,
    pub confidence: f64,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ExerciseStatus {
    VerifiedExact,
    Rejected,
    Error,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct RecordedCall {
    pub sequence: usize,
    pub args: Vec<String>,
    pub exit_code: i32,
    pub json: Option<Value>,
    pub stdout: String,
    pub stderr: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ExercisedCase {
    pub version: u64,
    pub status: ExerciseStatus,
    pub model: String,
    pub candidate: Candidate,
    pub task: TaskSpec,
    pub calls: Vec<RecordedCall>,
    pub changed_files: Vec<String>,
    pub undo_verified: bool,
    pub transcript: String,
    pub project_root: Option<String>,
    pub errors: Vec<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ComposedCase {
    pub version: u64,
    pub model: String,
    pub prompt_version: String,
    pub candidate: Candidate,
    pub composition: ModelDecision,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum VerificationStatus {
    VerifiedExact,
    Rejected,
    Error,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct VerifiedCase {
    pub version: u64,
    pub status: VerificationStatus,
    pub candidate_id: String,
    pub corpus: String,
    pub parent: String,
    pub commit: String,
    pub model: String,
    pub task: Option<TaskSpec>,
    pub changed_files: Vec<String>,
    pub undo_verified: bool,
    pub check_passed: Option<bool>,
    pub errors: Vec<String>,
}
