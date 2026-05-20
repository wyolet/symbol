package rpc

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sync"
)

// Handler is a typed JSON-RPC 2.0 method handler. Receives raw params,
// returns (result, error). Returning a non-nil JSONRPCError sends a
// JSON-RPC error response; returning a plain Go error sends ErrInternal.
type Handler func(params json.RawMessage) (any, error)

// JSONRPCError lets a handler return a JSON-RPC error with a specific
// code (e.g. ErrInvalidParams) instead of always ErrInternal.
type JSONRPCError struct {
	Code    int
	Message string
	Data    any
}

func (e *JSONRPCError) Error() string {
	return fmt.Sprintf("jsonrpc %d: %s", e.Code, e.Message)
}

// Server reads newline-delimited JSON-RPC 2.0 messages from `in`, dispatches
// to registered handlers, and writes responses to `out`. Concurrency-safe
// for writes; one request at a time on reads (workers don't need request
// pipelining for v1).
type Server struct {
	handlers map[string]Handler
	writeMu  sync.Mutex
	out      io.Writer
	in       io.Reader

	// Set to true by the shutdown notification; Serve returns cleanly.
	stopped bool
}

// NewServer constructs a server reading from `in` and writing to `out`.
func NewServer(in io.Reader, out io.Writer) *Server {
	return &Server{
		handlers: make(map[string]Handler),
		in:       in,
		out:      out,
	}
}

// Register attaches a method handler. Panics on duplicate registration —
// methods are static, drift here is a bug not user input.
func (s *Server) Register(method string, h Handler) {
	if _, exists := s.handlers[method]; exists {
		panic("rpc: duplicate handler for method " + method)
	}
	s.handlers[method] = h
}

// Serve runs the read-dispatch-respond loop until stdin closes or the
// shutdown notification fires. Returns nil on graceful exit.
func (s *Server) Serve() error {
	scanner := bufio.NewScanner(s.in)
	// Allow large `source` payloads (up to 32 MB) — JSON files, vendored
	// code, generated protobufs can be big.
	scanner.Buffer(make([]byte, 0, 64*1024), 32*1024*1024)

	for scanner.Scan() {
		if s.stopped {
			return nil
		}
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		s.handleLine(line)
	}
	if err := scanner.Err(); err != nil && !errors.Is(err, io.EOF) {
		return fmt.Errorf("rpc read: %w", err)
	}
	return nil
}

// Stop marks the server for shutdown after the current request finishes.
func (s *Server) Stop() { s.stopped = true }

func (s *Server) handleLine(line []byte) {
	var req Request
	if err := json.Unmarshal(line, &req); err != nil {
		s.writeError(nil, ErrParseError, "invalid JSON: "+err.Error())
		return
	}
	if req.JSONRPC != JSONRPCVersion {
		s.writeError(req.ID, ErrInvalidRequest, "jsonrpc must be \"2.0\"")
		return
	}
	handler, ok := s.handlers[req.Method]
	if !ok {
		// Notifications (no ID) are silently dropped on unknown method
		// per JSON-RPC 2.0; only requests get an error response.
		if len(req.ID) > 0 {
			s.writeError(req.ID, ErrMethodNotFound, "no such method: "+req.Method)
		}
		return
	}
	result, err := handler(req.Params)
	// Notifications never get a response, even on error.
	if len(req.ID) == 0 {
		return
	}
	if err != nil {
		var rpcErr *JSONRPCError
		if errors.As(err, &rpcErr) {
			s.writeError(req.ID, rpcErr.Code, rpcErr.Message)
			return
		}
		s.writeError(req.ID, ErrInternal, err.Error())
		return
	}
	s.writeResult(req.ID, result)
}

func (s *Server) writeResult(id json.RawMessage, result any) {
	s.writeMu.Lock()
	defer s.writeMu.Unlock()
	resp := Response{JSONRPC: JSONRPCVersion, ID: id, Result: result}
	s.encode(&resp)
}

func (s *Server) writeError(id json.RawMessage, code int, message string) {
	s.writeMu.Lock()
	defer s.writeMu.Unlock()
	resp := Response{
		JSONRPC: JSONRPCVersion,
		ID:      id,
		Error:   &Error{Code: code, Message: message},
	}
	s.encode(&resp)
}

func (s *Server) encode(resp *Response) {
	enc := json.NewEncoder(s.out)
	enc.SetEscapeHTML(false)
	// json.Encoder writes a trailing newline — exactly what our
	// newline-delimited framing needs.
	_ = enc.Encode(resp)
}
