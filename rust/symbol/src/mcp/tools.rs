//! Tool table: names, input schemas, descriptions and argument validation,
//! shaped exactly like the Python FastMCP server's (pydantic-generated
//! schemas, lax coercion of JSON arguments).

use std::sync::LazyLock;

use serde_json::{json, Map, Value};

use crate::pystr;

const DESCRIPTIONS_TOML: &str = include_str!("../../../../src/wyolet/symbol/data/mcp_tools.toml");

static DESCRIPTIONS: LazyLock<toml::Table> = LazyLock::new(|| {
    DESCRIPTIONS_TOML
        .parse()
        .expect("data/mcp_tools.toml parses")
});

#[derive(Clone, Copy, PartialEq)]
pub enum Ty {
    Str,
    OptStr,
    Bool,
    Int,
    OptInt,
    StrList,
    DictList,
}

#[derive(Clone, Copy)]
pub enum ParamDefault {
    Required,
    Null,
    Bool(bool),
    Int(i64),
}

pub struct Param {
    pub name: &'static str,
    pub ty: Ty,
    pub default: ParamDefault,
}

pub struct Tool {
    pub name: &'static str,
    /// The Python function name; pydantic titles schemas after it.
    pub func: &'static str,
    pub params: &'static [Param],
    /// Text-returning tools carry an output schema and `structuredContent`.
    pub text_output: bool,
}

const fn p(name: &'static str, ty: Ty, default: ParamDefault) -> Param {
    Param { name, ty, default }
}

const DRY_RUN: Param = p("dry_run", Ty::Bool, ParamDefault::Bool(false));
const FORCE: Param = p("force", Ty::Bool, ParamDefault::Bool(false));
const TARGET: Param = p("target", Ty::Str, ParamDefault::Required);
const VERBOSE: Param = p("verbose", Ty::Bool, ParamDefault::Bool(false));

/// tools/list order; tools the Python server also serves keep its
/// registration order.
pub const TOOLS: &[Tool] = &[
    Tool {
        name: "SearchSymbol",
        func: "search_symbol",
        params: &[
            p("patterns", Ty::StrList, ParamDefault::Required),
            p("kind", Ty::OptStr, ParamDefault::Null),
            p("file", Ty::OptStr, ParamDefault::Null),
            p("regex", Ty::Bool, ParamDefault::Bool(false)),
            p("fixed", Ty::Bool, ParamDefault::Bool(false)),
            p("ignore_case", Ty::Bool, ParamDefault::Bool(false)),
            p("limit", Ty::Int, ParamDefault::Int(100)),
        ],
        text_output: false,
    },
    Tool {
        name: "SymbolBody",
        func: "symbol_body",
        params: &[
            TARGET,
            p("include_refs", Ty::Bool, ParamDefault::Bool(false)),
            p("offset", Ty::Int, ParamDefault::Int(0)),
            p("limit", Ty::OptInt, ParamDefault::Null),
        ],
        text_output: false,
    },
    Tool {
        name: "SymbolOutline",
        func: "symbol_outline",
        params: &[TARGET],
        text_output: false,
    },
    Tool {
        name: "SymbolCallers",
        func: "symbol_callers",
        params: &[p("name", Ty::Str, ParamDefault::Required)],
        text_output: false,
    },
    Tool {
        name: "SymbolRefs",
        func: "symbol_refs",
        params: &[TARGET, VERBOSE],
        text_output: true,
    },
    Tool {
        name: "SymbolImplementers",
        func: "symbol_implementers",
        params: &[TARGET, VERBOSE],
        text_output: true,
    },
    Tool {
        name: "Patch",
        func: "patch",
        params: &[
            p("file", Ty::Str, ParamDefault::Required),
            p("range", Ty::Str, ParamDefault::Required),
            p("content", Ty::OptStr, ParamDefault::Null),
            FORCE,
            DRY_RUN,
        ],
        text_output: true,
    },
    Tool {
        name: "MultiPatch",
        func: "multi_patch",
        params: &[
            p("file", Ty::Str, ParamDefault::Required),
            p("edits", Ty::DictList, ParamDefault::Required),
            FORCE,
            DRY_RUN,
        ],
        text_output: true,
    },
    Tool {
        name: "DeleteSymbol",
        func: "delete_symbol",
        params: &[TARGET, FORCE, DRY_RUN],
        text_output: true,
    },
    Tool {
        name: "InsertSymbol",
        func: "insert_symbol",
        params: &[
            TARGET,
            p("position", Ty::Str, ParamDefault::Required),
            p("content", Ty::Str, ParamDefault::Required),
            p("reindent", Ty::Bool, ParamDefault::Bool(true)),
            DRY_RUN,
        ],
        text_output: true,
    },
    Tool {
        name: "RenameSymbol",
        func: "rename_symbol",
        params: &[
            TARGET,
            p("new_name", Ty::Str, ParamDefault::Required),
            DRY_RUN,
        ],
        text_output: true,
    },
    Tool {
        name: "Refresh",
        func: "refresh",
        params: &[
            p("full", Ty::Bool, ParamDefault::Bool(false)),
            p("keep_transactions", Ty::Bool, ParamDefault::Bool(false)),
        ],
        text_output: false,
    },
    Tool {
        name: "Undo",
        func: "undo",
        params: &[],
        text_output: false,
    },
    Tool {
        name: "ReplaceSymbol",
        func: "replace_symbol",
        params: &[
            TARGET,
            p("content", Ty::Str, ParamDefault::Required),
            DRY_RUN,
        ],
        text_output: true,
    },
];

pub fn find(name: &str) -> Option<&'static Tool> {
    TOOLS.iter().find(|t| t.name == name)
}

pub fn description(tool: &str) -> &'static str {
    DESCRIPTIONS
        .get(tool)
        .and_then(|t| t.get("description"))
        .and_then(|d| d.as_str())
        .unwrap_or_default()
}

/// pydantic's default field title: `dry_run` → `Dry Run`.
fn title(name: &str) -> String {
    name.split('_')
        .map(|word| {
            let mut chars = word.chars();
            match chars.next() {
                Some(first) => first.to_uppercase().chain(chars).collect(),
                None => String::new(),
            }
        })
        .collect::<Vec<String>>()
        .join(" ")
}

fn property(param: &Param) -> Value {
    let t = title(param.name);
    let nullable =
        |ty: &str| json!({"anyOf": [{"type": ty}, {"type": "null"}], "default": null, "title": t});
    match (param.ty, param.default) {
        (Ty::OptStr, _) => nullable("string"),
        (Ty::OptInt, _) => nullable("integer"),
        (Ty::Str, _) => json!({"title": t, "type": "string"}),
        (Ty::Bool, ParamDefault::Bool(b)) => json!({"default": b, "title": t, "type": "boolean"}),
        (Ty::Int, ParamDefault::Int(n)) => json!({"default": n, "title": t, "type": "integer"}),
        (Ty::Bool, _) => json!({"title": t, "type": "boolean"}),
        (Ty::Int, _) => json!({"title": t, "type": "integer"}),
        (Ty::StrList, _) => json!({"items": {"type": "string"}, "title": t, "type": "array"}),
        (Ty::DictList, _) => json!({
            "items": {"additionalProperties": true, "type": "object"},
            "title": t,
            "type": "array",
        }),
    }
}

/// One entry of `tools/list`.
pub fn definition(tool: &Tool) -> Value {
    let mut properties = Map::new();
    let mut required = vec![];
    for param in tool.params {
        properties.insert(param.name.into(), property(param));
        if matches!(param.default, ParamDefault::Required) {
            required.push(Value::from(param.name));
        }
    }
    let mut input = Map::new();
    input.insert("properties".into(), properties.into());
    if !required.is_empty() {
        input.insert("required".into(), required.into());
    }
    input.insert("title".into(), format!("{}Arguments", tool.func).into());
    input.insert("type".into(), "object".into());

    let mut def = Map::new();
    def.insert("name".into(), tool.name.into());
    def.insert("description".into(), description(tool.name).into());
    def.insert("inputSchema".into(), input.into());
    if tool.text_output {
        def.insert(
            "outputSchema".into(),
            json!({
                "properties": {"result": {"title": "Result", "type": "string"}},
                "required": ["result"],
                "title": format!("{}Output", tool.func),
                "type": "object",
            }),
        );
    }
    def.into()
}

// ---------------------------------------------------------- arguments

/// Validated arguments with defaults filled in.
#[derive(Debug)]
pub struct Args(Map<String, Value>);

impl Args {
    pub fn str(&self, name: &str) -> &str {
        self.0[name].as_str().unwrap_or_default()
    }

    pub fn opt_str(&self, name: &str) -> Option<&str> {
        self.0[name].as_str()
    }

    pub fn bool(&self, name: &str) -> bool {
        self.0[name].as_bool().unwrap_or(false)
    }

    pub fn int(&self, name: &str) -> i64 {
        self.0[name].as_i64().unwrap_or(0)
    }

    pub fn opt_int(&self, name: &str) -> Option<i64> {
        self.0[name].as_i64()
    }

    pub fn list(&self, name: &str) -> &[Value] {
        self.0[name].as_array().map(Vec::as_slice).unwrap_or(&[])
    }
}

struct Issue {
    loc: String,
    msg: &'static str,
    kind: &'static str,
    input: Value,
}

/// Coerce one value the way pydantic's lax mode does.
fn coerce(ty: Ty, value: &Value, loc: &str, issues: &mut Vec<Issue>) -> Value {
    let mut fail = |msg, kind| {
        issues.push(Issue {
            loc: loc.to_owned(),
            msg,
            kind,
            input: value.clone(),
        });
        Value::Null
    };
    match (ty, value) {
        (Ty::OptStr | Ty::OptInt, Value::Null) => Value::Null,
        (Ty::Str | Ty::OptStr, Value::String(_)) => value.clone(),
        (Ty::Str | Ty::OptStr, _) => fail("Input should be a valid string", "string_type"),
        (Ty::Bool, Value::Bool(_)) => value.clone(),
        (Ty::Bool, Value::Number(n)) => match n.as_f64() {
            Some(0.0) => false.into(),
            Some(1.0) => true.into(),
            _ => fail(
                "Input should be a valid boolean, unable to interpret input",
                "bool_parsing",
            ),
        },
        (Ty::Bool, Value::String(s)) => match s.to_ascii_lowercase().as_str() {
            "0" | "off" | "f" | "false" | "n" | "no" => false.into(),
            "1" | "on" | "t" | "true" | "y" | "yes" => true.into(),
            _ => fail(
                "Input should be a valid boolean, unable to interpret input",
                "bool_parsing",
            ),
        },
        (Ty::Bool, _) => fail("Input should be a valid boolean", "bool_type"),
        (Ty::Int | Ty::OptInt, Value::Number(n)) => match (n.as_i64(), n.as_f64()) {
            (Some(i), _) => i.into(),
            (None, Some(x)) if x.fract() == 0.0 && x.abs() < 9.2e18 => (x as i64).into(),
            _ => fail(
                "Input should be a valid integer, got a number with a fractional part",
                "int_from_float",
            ),
        },
        (Ty::Int | Ty::OptInt, Value::Bool(b)) => i64::from(*b).into(),
        (Ty::Int | Ty::OptInt, Value::String(s)) => match s.trim().parse::<i64>() {
            Ok(i) => i.into(),
            Err(_) => fail(
                "Input should be a valid integer, unable to parse string as an integer",
                "int_parsing",
            ),
        },
        (Ty::Int | Ty::OptInt, _) => fail("Input should be a valid integer", "int_type"),
        (Ty::StrList | Ty::DictList, Value::Array(items)) => {
            let item_ty = if ty == Ty::StrList {
                Ty::Str
            } else {
                Ty::DictList
            };
            let out: Vec<Value> = items
                .iter()
                .enumerate()
                .map(|(i, item)| {
                    let loc = format!("{loc}.{i}");
                    if item_ty == Ty::Str {
                        coerce(Ty::Str, item, &loc, issues)
                    } else if item.is_object() {
                        item.clone()
                    } else {
                        issues.push(Issue {
                            loc,
                            msg: "Input should be a valid dictionary",
                            kind: "dict_type",
                            input: item.clone(),
                        });
                        Value::Null
                    }
                })
                .collect();
            out.into()
        }
        (Ty::StrList | Ty::DictList, _) => fail("Input should be a valid list", "list_type"),
    }
}

/// Python `repr()` of a JSON-decoded value, as pydantic prints `input_value`.
fn py_repr(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => pystr::repr(s),
        Value::Array(items) => format!(
            "[{}]",
            items.iter().map(py_repr).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(map) => format!(
            "{{{}}}",
            map.iter()
                .map(|(k, v)| format!("{}: {}", pystr::repr(k), py_repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

fn py_type(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.is_f64() => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// Validate `arguments` against the tool's parameters. Unknown keys are
/// ignored; errors read like pydantic's ValidationError text.
pub fn parse_args(tool: &Tool, arguments: &Map<String, Value>) -> Result<Args, String> {
    let mut out = Map::new();
    let mut issues = vec![];
    for param in tool.params {
        let value = match (arguments.get(param.name), param.default) {
            (Some(value), _) => coerce(param.ty, value, param.name, &mut issues),
            (None, ParamDefault::Required) => {
                issues.push(Issue {
                    loc: param.name.into(),
                    msg: "Field required",
                    kind: "missing",
                    input: Value::Object(arguments.clone()),
                });
                Value::Null
            }
            (None, ParamDefault::Null) => Value::Null,
            (None, ParamDefault::Bool(b)) => b.into(),
            (None, ParamDefault::Int(n)) => n.into(),
        };
        out.insert(param.name.into(), value);
    }
    if issues.is_empty() {
        return Ok(Args(out));
    }
    let plural = if issues.len() == 1 { "" } else { "s" };
    let mut text = format!(
        "{} validation error{plural} for {}Arguments",
        issues.len(),
        tool.func
    );
    for issue in &issues {
        text.push_str(&format!(
            "\n{}\n  {} [type={}, input_value={}, input_type={}]",
            issue.loc,
            issue.msg,
            issue.kind,
            py_repr(&issue.input),
            py_type(&issue.input)
        ));
    }
    Err(text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_tool_has_a_description() {
        for tool in TOOLS {
            assert!(!description(tool.name).is_empty(), "{}", tool.name);
        }
    }

    #[test]
    fn schema_matches_pydantic_shape() {
        let def = definition(find("SymbolBody").unwrap());
        assert_eq!(
            def["inputSchema"],
            json!({
                "properties": {
                    "target": {"title": "Target", "type": "string"},
                    "include_refs": {"default": false, "title": "Include Refs", "type": "boolean"},
                    "offset": {"default": 0, "title": "Offset", "type": "integer"},
                    "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": null, "title": "Limit"},
                },
                "required": ["target"],
                "title": "symbol_bodyArguments",
                "type": "object",
            })
        );
        assert!(def.get("outputSchema").is_none());
        let undo = definition(find("Undo").unwrap());
        assert!(undo["inputSchema"].get("required").is_none());
        assert_eq!(
            definition(find("Patch").unwrap())["outputSchema"]["title"],
            "patchOutput"
        );
    }

    #[test]
    fn lax_coercion_and_errors() {
        let tool = find("SearchSymbol").unwrap();
        let args = json!({"patterns": ["a"], "limit": "5", "regex": "yes", "extra": 1});
        let parsed = parse_args(tool, args.as_object().unwrap()).unwrap();
        assert_eq!(parsed.int("limit"), 5);
        assert!(parsed.bool("regex"));
        assert_eq!(parsed.opt_str("kind"), None);

        let err = parse_args(tool, &Map::new()).unwrap_err();
        assert_eq!(
            err,
            "1 validation error for search_symbolArguments\npatterns\n  Field required \
             [type=missing, input_value={}, input_type=dict]"
        );
        let bad = json!({"patterns": "x", "limit": 1.5});
        let err = parse_args(tool, bad.as_object().unwrap()).unwrap_err();
        assert!(err.starts_with("2 validation errors for search_symbolArguments\npatterns\n"));
        assert!(err.contains("input_value='x', input_type=str"));
    }
}
