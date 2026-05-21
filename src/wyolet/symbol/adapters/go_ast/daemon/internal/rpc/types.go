// Package rpc defines the wire types for the symbol language-adapter
// JSON-RPC 2.0 protocol. Hand-synced with schemas/symbol.rpc.schema.json
// and src/wyolet/symbol/protocols/types.py. CI validates both sides.
package rpc

import "encoding/json"

// ── JSON-RPC 2.0 envelope ──────────────────────────────────────────

const JSONRPCVersion = "2.0"

// Request is an incoming JSON-RPC 2.0 request. ID is nil for notifications.
type Request struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

// Response is an outgoing JSON-RPC 2.0 response. Either Result or Error is set.
type Response struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Result  any             `json:"result,omitempty"`
	Error   *Error          `json:"error,omitempty"`
}

// Error follows JSON-RPC 2.0 error object shape.
type Error struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
	Data    any    `json:"data,omitempty"`
}

// JSON-RPC 2.0 reserved error codes.
const (
	ErrParseError     = -32700
	ErrInvalidRequest = -32600
	ErrMethodNotFound = -32601
	ErrInvalidParams  = -32602
	ErrInternal       = -32603
)

// ── method params / results ────────────────────────────────────────

// InitializeParams is the handshake input from host to worker.
type InitializeParams struct {
	ProtocolVersion string `json:"protocol_version"`
}

// InitializeResult is the worker's handshake reply.
type InitializeResult struct {
	Language       string   `json:"language"`
	WorkerVersion  string   `json:"worker_version"`
	Capabilities   []string `json:"capabilities"`
}

// ScanFileParams is the input to scan_file.
type ScanFileParams struct {
	Path         string `json:"path"`
	Source       string `json:"source"`
	ModulePrefix string `json:"module_prefix,omitempty"`
}

// ValidateSyntaxParams is the input to validate_syntax.
type ValidateSyntaxParams struct {
	Source string `json:"source"`
}

// SignatureParams is the input to ``signature``.
type SignatureParams struct {
	Source string `json:"source"`
}

// SignatureResult is the canonical Go declaration of the first
// top-level symbol in the input, formatted by go/printer with the body
// stripped. Empty if the input has no top-level declarations.
type SignatureResult struct {
	Signature string `json:"signature"`
}

// ── wire data types (mirror schemas/symbol.rpc.schema.json $defs) ──

// ScannedRef is one name reference inside a symbol's body.
type ScannedRef struct {
	Name string `json:"name"`
	Kind string `json:"kind"` // "name" or "attr"
	Line int    `json:"line"`
}

// ScannedImport is one per-alias import binding.
type ScannedImport struct {
	Local  string `json:"local"`
	Source string `json:"source"`
	Line   int    `json:"line"`
}

// ScannedSymbol is one declared symbol plus refs from its direct scope.
// Children carry nested symbols (a class's methods, a struct's nothing
// in Go — Go's methods are siblings of the receiver type).
type ScannedSymbol struct {
	Kind          string          `json:"kind"`
	Name          string          `json:"name"`
	QualifiedPath string          `json:"qualified_path"`
	ByteRange     [2]int          `json:"byte_range"`
	LineRange     [2]int          `json:"line_range"`
	Refs          []ScannedRef    `json:"refs"`
	Children      []ScannedSymbol `json:"children"`
}

// FileScan is the full scan of one file.
type FileScan struct {
	Language string          `json:"language"`
	OK       bool            `json:"ok"`
	Error    *string         `json:"error,omitempty"`
	Imports  []ScannedImport `json:"imports"`
	Symbols  []ScannedSymbol `json:"symbols"`
}

// ParseResult is the result of a cheap syntax-only check.
type ParseResult struct {
	OK           bool    `json:"ok"`
	ErrorLine    *int    `json:"error_line,omitempty"`
	ErrorMessage *string `json:"error_message,omitempty"`
}

// ── rename ─────────────────────────────────────────────────────────

// RenameMemberParams is the input to rename_member.
//
// The daemon loads the project under ProjectRoot via packages.Load with
// full type info, then classifies every SelectorExpr matching Leaf
// against TargetOwnerQpath using info.Selections. Receiver-type
// resolution is exact (uses go/types) — no tier-1 limits.
type RenameMemberParams struct {
	ProjectRoot      string   `json:"project_root"`
	Leaf             string   `json:"leaf"`
	NewName          string   `json:"new_name"`
	TargetQpath      string   `json:"target_qpath"`
	TargetOwnerQpath string   `json:"target_owner_qpath"`
	DeclaringFile    string   `json:"declaring_file"`
	DeclByteRange    [2]int   `json:"decl_byte_range"`
	CandidateFiles   []string `json:"candidate_files"`
}

// RenameModuleBindingParams is the input to rename_module_binding.
type RenameModuleBindingParams struct {
	ProjectRoot       string   `json:"project_root"`
	Leaf              string   `json:"leaf"`
	NewName           string   `json:"new_name"`
	TargetQpath       string   `json:"target_qpath"`
	TargetModuleQpath string   `json:"target_module_qpath"`
	DeclaringFile     string   `json:"declaring_file"`
	DeclByteRange     [2]int   `json:"decl_byte_range"`
	CandidateFiles    []string `json:"candidate_files"`
}

// ByteRewrite is one identifier-token rewrite. Mirrors the neutral
// protocols.types.ByteRewrite.
type ByteRewrite struct {
	ByteStart      int    `json:"byte_start"`
	ByteEnd        int    `json:"byte_end"`
	NewText        string `json:"new_text"`
	Line           int    `json:"line"`
	Col            int    `json:"col"`
	ReceiverSource string `json:"receiver_source"`
}

// SkippedMismatchSite is a leaf hit correctly identified as a
// different declaration (different receiver type, different package).
type SkippedMismatchSite struct {
	ByteStart        int    `json:"byte_start"`
	ByteEnd          int    `json:"byte_end"`
	Line             int    `json:"line"`
	Col              int    `json:"col"`
	ReceiverSource   string `json:"receiver_source"`
	ResolvedToQpath  string `json:"resolved_to_qpath"`
}

// UnresolvedSite is a leaf hit the daemon could not classify. Should
// be rare with go/types — surface for agent review.
type UnresolvedSite struct {
	ByteStart      int    `json:"byte_start"`
	ByteEnd        int    `json:"byte_end"`
	Line           int    `json:"line"`
	Col            int    `json:"col"`
	ReceiverSource string `json:"receiver_source"`
	Why            string `json:"why"`
}

// RenameFileAnalysis is the three-bucket result for one file.
type RenameFileAnalysis struct {
	Rewrites        []ByteRewrite         `json:"rewrites"`
	SkippedMismatch []SkippedMismatchSite `json:"skipped_mismatch"`
	Unresolved      []UnresolvedSite      `json:"unresolved"`
}

// RenameResult maps file relative paths to per-file analyses. Files
// the daemon analyzed but produced no hits for may be omitted.
type RenameResult struct {
	Files map[string]RenameFileAnalysis `json:"files"`
}
