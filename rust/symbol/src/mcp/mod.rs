//! `symbol mcp` — the MCP server over stdio: newline-delimited JSON-RPC 2.0,
//! one project root per server. Wire shapes (initialize result, tool list,
//! tool results and tool errors) follow the Python FastMCP server so either
//! implementation can back the same client configuration.

mod handlers;
mod limits;
mod render;
mod state;
mod tools;

use std::io::{BufRead, Write};
use std::path::Path;

use anyhow::Result;
use serde_json::{json, Map, Value};

pub use state::Session;

const LATEST_PROTOCOL_VERSION: &str = "2025-11-25";
const SUPPORTED_PROTOCOL_VERSIONS: &[&str] = &[
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    LATEST_PROTOCOL_VERSION,
];

const PARSE_ERROR: i64 = -32700;
const INVALID_REQUEST: i64 = -32600;
const METHOD_NOT_FOUND: i64 = -32601;
const INVALID_PARAMS: i64 = -32602;

/// Blocking stdio loop; returns when the client closes stdin.
pub fn serve(root: &Path) -> Result<()> {
    limits::apply_nice();
    limits::apply_hard_mem_limit();
    limits::install_watchdog();

    let (mut session, loaded) = Session::start(root);
    match (&loaded, session.loaded_index()) {
        (Ok(()), Some(index)) => eprintln!(
            "[symbol-mcp] root={} index={} files={} symbols={} status={}",
            root.display(),
            session.index_source,
            index.files.len(),
            index.symbols.len(),
            if index.symbols.is_empty() {
                "empty"
            } else {
                "ok"
            },
        ),
        (Err(err), _) => eprintln!("[symbol-mcp] root={} index=error: {err:#}", root.display()),
        _ => {}
    }

    let stdin = std::io::stdin();
    let mut stdout = std::io::stdout().lock();
    for line in stdin.lock().lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        if let Some(reply) = handle_line(&mut session, &line) {
            serde_json::to_writer(&mut stdout, &reply)?;
            stdout.write_all(b"\n")?;
            stdout.flush()?;
        }
    }
    Ok(())
}

/// One incoming line → the reply to write, if any (notifications and
/// client responses get none). Batches are answered with an array.
pub fn handle_line(session: &mut Session, line: &str) -> Option<Value> {
    let message: Value = match serde_json::from_str(line) {
        Ok(message) => message,
        Err(err) => return Some(error_reply(Value::Null, PARSE_ERROR, &err.to_string())),
    };
    match message {
        Value::Array(batch) => {
            let replies: Vec<Value> = batch
                .into_iter()
                .filter_map(|m| handle_message(session, m))
                .collect();
            (!replies.is_empty()).then(|| replies.into())
        }
        single => handle_message(session, single),
    }
}

fn handle_message(session: &mut Session, message: Value) -> Option<Value> {
    let Value::Object(message) = message else {
        return Some(error_reply(Value::Null, INVALID_REQUEST, "Invalid request"));
    };
    let Some(method) = message.get("method").and_then(Value::as_str) else {
        // A response to a request we never send; nothing to do.
        return None;
    };
    let id = message.get("id").cloned()?;
    let empty = Map::new();
    let params = message
        .get("params")
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    let result = match method {
        "initialize" => initialize(params),
        "ping" => json!({}),
        "tools/list" => {
            json!({"tools": tools::TOOLS.iter().map(tools::definition).collect::<Vec<_>>()})
        }
        "tools/call" => match call_tool(session, params) {
            Ok(result) => result,
            Err(message) => return Some(error_reply(id, INVALID_PARAMS, &message)),
        },
        "resources/list" => json!({"resources": []}),
        "resources/templates/list" => json!({"resourceTemplates": []}),
        "prompts/list" => json!({"prompts": []}),
        _ => return Some(error_reply(id, METHOD_NOT_FOUND, "Method not found")),
    };
    Some(json!({"jsonrpc": "2.0", "id": id, "result": result}))
}

fn error_reply(id: Value, code: i64, message: &str) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})
}

/// Echo the client's protocol version when supported, else offer ours.
fn initialize(params: &Map<String, Value>) -> Value {
    let requested = params
        .get("protocolVersion")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let version = if SUPPORTED_PROTOCOL_VERSIONS.contains(&requested) {
        requested
    } else {
        LATEST_PROTOCOL_VERSION
    };
    json!({
        "protocolVersion": version,
        "capabilities": {
            "experimental": {},
            "prompts": {"listChanged": false},
            "resources": {"subscribe": false, "listChanged": false},
            "tools": {"listChanged": false},
        },
        "serverInfo": {"name": "symbol", "version": env!("CARGO_PKG_VERSION")},
    })
}

fn tool_error(text: String) -> Value {
    json!({"content": [{"type": "text", "text": text}], "isError": true})
}

/// A `CallToolResult`. Failures inside a tool are results with `isError`,
/// not JSON-RPC errors, so the agent sees the message.
fn call_tool(session: &mut Session, params: &Map<String, Value>) -> Result<Value, String> {
    let Some(name) = params.get("name").and_then(Value::as_str) else {
        return Err("tools/call requires a tool name".into());
    };
    let Some(tool) = tools::find(name) else {
        return Ok(tool_error(format!("Unknown tool: {name}")));
    };
    let empty = Map::new();
    let arguments = params
        .get("arguments")
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    let outcome = tools::parse_args(tool, arguments)
        .map_err(anyhow::Error::msg)
        .and_then(|args| handlers::call(name, session, &args));
    Ok(match outcome {
        Ok(handlers::ToolOutput::Json(value)) => {
            let text = serde_json::to_string_pretty(&value).unwrap_or_default();
            json!({"content": [{"type": "text", "text": text}], "isError": false})
        }
        Ok(handlers::ToolOutput::Text(text)) => json!({
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"result": text},
            "isError": false,
        }),
        Err(err) => tool_error(format!("Error executing tool {name}: {err:#}")),
    })
}
