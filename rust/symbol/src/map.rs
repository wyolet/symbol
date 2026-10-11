//! Module import graph over the symbol index. Nodes are modules (a Go
//! package, a Python module); edges are imports whose source names a module
//! defined in the project. Built from index records only.

use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};

use serde_json::json;
use symbol_linguist::fnmatch::GlobPat;

use crate::index::{Index, S_PARENT, S_PATH};

#[derive(PartialEq, Eq, PartialOrd, Ord)]
pub struct ImportSite {
    pub file: String,
    pub line: i64,
}

pub struct ModuleGraph {
    /// Sorted module names; a node id is an index into this list.
    pub modules: Vec<String>,
    pub files: Vec<Vec<String>>,
    /// Out-edges per node, each with the import sites that create it.
    pub imports: Vec<BTreeMap<usize, BTreeSet<ImportSite>>>,
    pub external: Vec<BTreeSet<String>>,
    /// Sources that name a project namespace (or nothing) but no module.
    pub unresolved: Vec<BTreeSet<String>>,
    /// Files with imports whose module can't be derived (no symbols).
    pub unattributed: Vec<String>,
}

enum Target {
    Module(String),
    External,
    Unresolved,
}

/// Qualified paths are `<module>.<name>`, with methods possibly nested
/// under a type (`<module>.<Type>.<name>`) at top level. Strip trailing
/// segments while they still name a symbol; what remains is the module.
fn file_module(idx: &Index, file_id: i64) -> Option<String> {
    let rows = idx.by_file.get(&file_id)?;
    let mut votes: HashMap<&str, usize> = HashMap::new();
    for &row in rows {
        let sym = &idx.symbols[row as usize];
        if sym[S_PATH] == 0 || sym[S_PARENT] != -1 {
            continue;
        }
        let mut scope = idx.path_of(row);
        while let Some((head, _)) = scope.rsplit_once('.') {
            scope = head;
            if !idx.by_path.contains_key(scope) {
                *votes.entry(scope).or_default() += 1;
                break;
            }
        }
    }
    votes
        .into_iter()
        .max_by(|a, b| {
            a.1.cmp(&b.1)
                .then(b.0.len().cmp(&a.0.len()))
                .then(b.0.cmp(a.0))
        })
        .map(|(m, _)| m.to_owned())
}

/// Every strict prefix of a module name cut at a `.` or `/` boundary.
fn namespaces<'a>(modules: impl Iterator<Item = &'a String>) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for m in modules {
        for (i, c) in m.char_indices() {
            if c == '.' || c == '/' {
                out.insert(m[..i].to_owned());
            }
        }
    }
    out
}

impl ModuleGraph {
    pub fn from_index(idx: &Index, exclude: &[GlobPat]) -> ModuleGraph {
        let mut module_of: Vec<Option<String>> = Vec::with_capacity(idx.files.len());
        let mut all_modules: BTreeSet<String> = BTreeSet::new();
        let mut kept: BTreeMap<String, Vec<String>> = BTreeMap::new();
        let mut unattributed = vec![];
        for (fid, rel) in idx.files.iter().enumerate() {
            let module = file_module(idx, fid as i64);
            let excluded = exclude.iter().any(|g| g.matches(rel));
            match &module {
                Some(m) => {
                    all_modules.insert(m.clone());
                    if !excluded {
                        kept.entry(m.clone()).or_default().push(rel.clone());
                    }
                }
                None => {
                    if !excluded && !idx.imports_for(fid as i64).is_empty() {
                        unattributed.push(rel.clone());
                    }
                }
            }
            module_of.push(if excluded { None } else { module });
        }
        let ns = namespaces(all_modules.iter());
        let resolve = |local: &str, source: &str| -> Target {
            let submodule = format!("{source}.{local}");
            if all_modules.contains(&submodule) {
                Target::Module(submodule)
            } else if all_modules.contains(source) {
                Target::Module(source.to_owned())
            } else if source.is_empty() || ns.contains(source) {
                Target::Unresolved
            } else {
                Target::External
            }
        };

        let modules: Vec<String> = kept.keys().cloned().collect();
        let ids: HashMap<&str, usize> = modules
            .iter()
            .enumerate()
            .map(|(i, m)| (m.as_str(), i))
            .collect();
        let n = modules.len();
        let mut graph = ModuleGraph {
            files: kept.into_values().collect(),
            imports: (0..n).map(|_| BTreeMap::new()).collect(),
            external: vec![BTreeSet::new(); n],
            unresolved: vec![BTreeSet::new(); n],
            unattributed,
            modules: vec![],
        };
        for (fid, module) in module_of.iter().enumerate() {
            let Some(module) = module else { continue };
            let src = ids[module.as_str()];
            let rel = &idx.files[fid];
            for (local, source, line) in idx.imports_for(fid as i64) {
                match resolve(local, source) {
                    Target::Module(target) => {
                        // Targets whose files are all excluded drop out.
                        let Some(&dst) = ids.get(target.as_str()) else {
                            continue;
                        };
                        if dst == src {
                            continue;
                        }
                        graph.imports[src]
                            .entry(dst)
                            .or_default()
                            .insert(ImportSite {
                                file: rel.clone(),
                                line,
                            });
                    }
                    Target::External => {
                        graph.external[src].insert(source.to_owned());
                    }
                    Target::Unresolved => {
                        graph.unresolved[src].insert(source.to_owned());
                    }
                }
            }
        }
        graph.modules = modules;
        graph
    }

    /// External sources that equal a project module's trailing segments.
    /// They may be relative or short imports the index can't resolve, so
    /// counting them as external would understate the graph.
    pub fn external_matching_modules(&self) -> Vec<(String, Vec<usize>)> {
        let sources: BTreeSet<&String> = self.external.iter().flatten().collect();
        sources
            .into_iter()
            .filter_map(|source| {
                let hits: Vec<usize> = (0..self.modules.len())
                    .filter(|&i| ends_at_boundary(&self.modules[i], source))
                    .collect();
                (!hits.is_empty()).then(|| (source.clone(), hits))
            })
            .collect()
    }

    pub fn edge_count(&self) -> usize {
        self.imports.iter().map(BTreeMap::len).sum()
    }

    fn adjacency(&self) -> Vec<Vec<usize>> {
        self.imports
            .iter()
            .map(|m| m.keys().copied().collect())
            .collect()
    }

    fn reverse_adjacency(&self) -> Vec<Vec<usize>> {
        let mut rev = vec![vec![]; self.modules.len()];
        for (src, targets) in self.imports.iter().enumerate() {
            for &dst in targets.keys() {
                rev[dst].push(src);
            }
        }
        rev
    }

    pub fn fan_in(&self) -> Vec<usize> {
        self.reverse_adjacency().iter().map(Vec::len).collect()
    }

    /// Strongly connected components with more than one module, each as a
    /// shortest cycle through its first member.
    pub fn cycles(&self) -> Vec<(Vec<usize>, Vec<usize>)> {
        let adj = self.adjacency();
        let mut out: Vec<(Vec<usize>, Vec<usize>)> = strongly_connected(&adj)
            .into_iter()
            .filter(|c| c.len() > 1)
            .map(|c| {
                let path = shortest_cycle(&adj, &c);
                (c, path)
            })
            .collect();
        out.sort_by(|a, b| b.0.len().cmp(&a.0.len()).then(a.0.cmp(&b.0)));
        out
    }

    /// Longest import chains, computed on the component DAG so cycles
    /// cannot inflate them. Chains that start inside an already reported
    /// chain are skipped.
    pub fn deep_chains(&self, limit: usize) -> Vec<Vec<usize>> {
        let adj = self.adjacency();
        let comps = strongly_connected(&adj);
        let mut comp_of = vec![0; adj.len()];
        for (c, members) in comps.iter().enumerate() {
            for &m in members {
                comp_of[m] = c;
            }
        }
        // Tarjan emits components in reverse topological order, so every
        // successor component is final before its predecessors.
        let mut length = vec![1usize; comps.len()];
        let mut next: Vec<Option<usize>> = vec![None; comps.len()];
        for (c, members) in comps.iter().enumerate() {
            for &m in members {
                for &w in &adj[m] {
                    let d = comp_of[w];
                    if d == c {
                        continue;
                    }
                    let better = length[d] + 1 > length[c]
                        || (length[d] + 1 == length[c]
                            && next[c].is_some_and(|cur| comps[d][0] < comps[cur][0]));
                    if better {
                        length[c] = length[d] + 1;
                        next[c] = Some(d);
                    }
                }
            }
        }
        let mut order: Vec<usize> = (0..comps.len()).filter(|&c| length[c] > 1).collect();
        order.sort_by(|&a, &b| {
            length[b]
                .cmp(&length[a])
                .then(comps[a][0].cmp(&comps[b][0]))
        });
        let mut covered = vec![false; comps.len()];
        let mut chains = vec![];
        for start in order {
            if covered[start] {
                continue;
            }
            let mut chain = vec![];
            let mut cur = Some(start);
            while let Some(c) = cur {
                covered[c] = true;
                chain.push(comps[c][0]);
                cur = next[c];
            }
            chains.push(chain);
            if limit != 0 && chains.len() >= limit {
                break;
            }
        }
        chains
    }

    /// Breadth-first closure: (module, depth, via) for every module reached
    /// from `start`, following imports forward or, with `reverse`, importers.
    pub fn closure(
        &self,
        start: usize,
        reverse: bool,
        max_depth: Option<usize>,
    ) -> Vec<(usize, usize, usize)> {
        let adj = if reverse {
            self.reverse_adjacency()
        } else {
            self.adjacency()
        };
        let mut seen = vec![false; adj.len()];
        seen[start] = true;
        let mut queue = VecDeque::from([(start, 0usize)]);
        let mut out = vec![];
        while let Some((node, depth)) = queue.pop_front() {
            if max_depth.is_some_and(|d| depth >= d) {
                continue;
            }
            for &next in &adj[node] {
                if !seen[next] {
                    seen[next] = true;
                    out.push((next, depth + 1, node));
                    queue.push_back((next, depth + 1));
                }
            }
        }
        out.sort_by(|a, b| a.1.cmp(&b.1).then(a.0.cmp(&b.0)));
        out
    }

    /// Exact module name, an indexed file path, or a unique suffix at a
    /// `/` or `.` boundary (a path relative to the module root).
    pub fn find(&self, idx: &Index, query: &str) -> Result<usize, Vec<String>> {
        let query = query.trim_start_matches("./").trim_end_matches('/');
        if let Ok(i) = self.modules.binary_search_by(|m| m.as_str().cmp(query)) {
            return Ok(i);
        }
        if let Some(fid) = idx.files.iter().position(|f| f == query) {
            if let Some(m) = file_module(idx, fid as i64) {
                if let Ok(i) = self.modules.binary_search(&m) {
                    return Ok(i);
                }
            }
        }
        let matches: Vec<usize> = (0..self.modules.len())
            .filter(|&i| ends_at_boundary(&self.modules[i], query))
            .collect();
        match matches.as_slice() {
            [one] => Ok(*one),
            _ => Err(matches.iter().map(|&i| self.modules[i].clone()).collect()),
        }
    }
}

/// `name` ends with `suffix` and a `/` or `.` precedes it.
fn ends_at_boundary(name: &str, suffix: &str) -> bool {
    !suffix.is_empty()
        && name.len() > suffix.len()
        && name.ends_with(suffix)
        && matches!(name.as_bytes()[name.len() - suffix.len() - 1], b'/' | b'.')
}

/// Iterative Tarjan; components come out in reverse topological order,
/// members sorted.
fn strongly_connected(adj: &[Vec<usize>]) -> Vec<Vec<usize>> {
    let n = adj.len();
    let mut index = vec![usize::MAX; n];
    let mut low = vec![0; n];
    let mut on_stack = vec![false; n];
    let mut stack = vec![];
    let mut out = vec![];
    let mut counter = 0;
    for root in 0..n {
        if index[root] != usize::MAX {
            continue;
        }
        index[root] = counter;
        low[root] = counter;
        counter += 1;
        stack.push(root);
        on_stack[root] = true;
        let mut calls: Vec<(usize, usize)> = vec![(root, 0)];
        while let Some(&(v, i)) = calls.last() {
            if i < adj[v].len() {
                calls.last_mut().unwrap().1 += 1;
                let w = adj[v][i];
                if index[w] == usize::MAX {
                    index[w] = counter;
                    low[w] = counter;
                    counter += 1;
                    stack.push(w);
                    on_stack[w] = true;
                    calls.push((w, 0));
                } else if on_stack[w] {
                    low[v] = low[v].min(index[w]);
                }
                continue;
            }
            calls.pop();
            if let Some(&(parent, _)) = calls.last() {
                low[parent] = low[parent].min(low[v]);
            }
            if low[v] == index[v] {
                let mut comp = vec![];
                while let Some(w) = stack.pop() {
                    on_stack[w] = false;
                    comp.push(w);
                    if w == v {
                        break;
                    }
                }
                comp.sort_unstable();
                out.push(comp);
            }
        }
    }
    out
}

/// Shortest cycle through `comp[0]` using only edges inside `comp`;
/// first module repeated at the end.
fn shortest_cycle(adj: &[Vec<usize>], comp: &[usize]) -> Vec<usize> {
    let start = comp[0];
    let inside = |w: &usize| comp.binary_search(w).is_ok();
    let mut parent: HashMap<usize, usize> = HashMap::new();
    let mut queue = VecDeque::from([start]);
    while let Some(node) = queue.pop_front() {
        for &next in adj[node].iter().filter(|w| inside(w)) {
            if next == start {
                let mut back = vec![];
                let mut cur = node;
                while cur != start {
                    back.push(cur);
                    cur = parent[&cur];
                }
                let mut path = vec![start];
                path.extend(back.into_iter().rev());
                path.push(start);
                return path;
            }
            if let std::collections::hash_map::Entry::Vacant(e) = parent.entry(next) {
                e.insert(node);
                queue.push_back(next);
            }
        }
    }
    vec![start]
}

// ---------------------------------------------------------- command

pub struct MapOptions {
    pub dependents: Option<String>,
    pub deps: Option<String>,
    pub depth: Option<usize>,
    pub limit: usize,
    pub exclude: Vec<String>,
    pub verbose: bool,
    pub json: bool,
}

/// Longest common prefix of all names, cut after a `/` or `.` boundary;
/// text output prints names relative to it.
fn shared_prefix(modules: &[String]) -> String {
    let (Some(first), Some(last)) = (modules.first(), modules.last()) else {
        return String::new();
    };
    if modules.len() < 2 {
        return String::new();
    }
    // Names are sorted, so the first and last bound the common prefix.
    let mut common = first
        .bytes()
        .zip(last.bytes())
        .take_while(|(a, b)| a == b)
        .count();
    while !first.is_char_boundary(common) {
        common -= 1;
    }
    // A root module (e.g. a Go module's top package) is the prefix itself.
    let ends_at_boundary = modules
        .iter()
        .all(|m| m.len() == common || matches!(m.as_bytes()[common], b'/' | b'.'));
    if ends_at_boundary {
        if let Some(longer) = modules.iter().find(|m| m.len() > common) {
            return longer[..=common].to_owned();
        }
    }
    first[..common]
        .rfind(['/', '.'])
        .map(|i| first[..=i].to_owned())
        .unwrap_or_default()
}

fn capped<T>(items: &[T], limit: usize) -> &[T] {
    if limit == 0 || items.len() <= limit {
        items
    } else {
        &items[..limit]
    }
}

/// Run `symbol map`; returns false when a requested module can't be resolved.
pub fn run(idx: &Index, opts: &MapOptions) -> bool {
    let exclude: Vec<GlobPat> = opts
        .exclude
        .iter()
        .filter_map(|p| GlobPat::new(p))
        .collect();
    let graph = ModuleGraph::from_index(idx, &exclude);
    let prefix = shared_prefix(&graph.modules);
    let short = |i: usize| -> String {
        let m = &graph.modules[i];
        match m.strip_prefix(prefix.as_str()) {
            Some(rest) if !rest.is_empty() => rest.to_owned(),
            _ => m.clone(),
        }
    };

    if let Some((query, reverse)) = opts
        .dependents
        .as_deref()
        .map(|q| (q, true))
        .or(opts.deps.as_deref().map(|q| (q, false)))
    {
        return print_closure(idx, &graph, query, reverse, opts, &short);
    }

    let files_total: usize = graph.files.iter().map(Vec::len).sum();
    let external_total: BTreeSet<&String> = graph.external.iter().flatten().collect();
    let unresolved_total: BTreeSet<&String> = graph.unresolved.iter().flatten().collect();
    let lookalikes = graph.external_matching_modules();
    let fan_in = graph.fan_in();
    let mut by_fan_in: Vec<usize> = (0..graph.modules.len())
        .filter(|&i| fan_in[i] > 0)
        .collect();
    by_fan_in.sort_by(|&a, &b| fan_in[b].cmp(&fan_in[a]).then(a.cmp(&b)));
    let mut by_fan_out: Vec<usize> = (0..graph.modules.len())
        .filter(|&i| !graph.imports[i].is_empty())
        .collect();
    by_fan_out.sort_by(|&a, &b| {
        graph.imports[b]
            .len()
            .cmp(&graph.imports[a].len())
            .then(a.cmp(&b))
    });
    let cycles = graph.cycles();
    let chain_limit = if opts.verbose {
        opts.limit
    } else {
        opts.limit.min(3)
    };
    let chains = graph.deep_chains(chain_limit);

    if opts.json {
        let name = |i: &usize| graph.modules[*i].clone();
        let out = json!({
            "total_modules": graph.modules.len(),
            "total_files": files_total,
            "total_edges": graph.edge_count(),
            "external_sources": external_total.len(),
            "cycles": cycles.iter().map(|(members, path)| json!({
                "modules": members.iter().map(name).collect::<Vec<_>>(),
                "path": path.iter().map(name).collect::<Vec<_>>(),
                "edges": path.windows(2).map(|w| json!({
                    "from": name(&w[0]),
                    "to": name(&w[1]),
                    "sites": graph.imports[w[0]][&w[1]].iter()
                        .map(|s| json!({"file": s.file, "line": s.line}))
                        .collect::<Vec<_>>(),
                })).collect::<Vec<_>>(),
            })).collect::<Vec<_>>(),
            "hotspots": capped(&by_fan_in, opts.limit).iter().map(|i| json!({
                "module": name(i), "fan_in": fan_in[*i],
            })).collect::<Vec<_>>(),
            "fragile": capped(&by_fan_out, opts.limit).iter().map(|i| json!({
                "module": name(i), "fan_out": graph.imports[*i].len(),
                "external": graph.external[*i].len(),
            })).collect::<Vec<_>>(),
            "deep_chains": chains.iter().map(|c| json!({
                "length": c.len(),
                "chain": c.iter().map(name).collect::<Vec<_>>(),
            })).collect::<Vec<_>>(),
            "modules": (0..graph.modules.len()).map(|i| json!({
                "module": graph.modules[i],
                "files": graph.files[i],
                "fan_in": fan_in[i],
                "imports": graph.imports[i].keys().map(name).collect::<Vec<_>>(),
                "external": graph.external[i],
                "unresolved": graph.unresolved[i],
            })).collect::<Vec<_>>(),
            "unattributed_files": graph.unattributed,
            "external_matching_modules": lookalikes.iter().map(|(source, hits)| json!({
                "source": source,
                "modules": hits.iter().map(name).collect::<Vec<_>>(),
            })).collect::<Vec<_>>(),
        });
        println!("{}", serde_json::to_string_pretty(&out).unwrap_or_default());
        return true;
    }

    if graph.modules.is_empty() {
        println!(
            "no modules: the index has {} files but none define symbols",
            idx.files.len()
        );
        return true;
    }
    println!(
        "{} modules, {} files, {} import edges, {} external sources",
        graph.modules.len(),
        files_total,
        graph.edge_count(),
        external_total.len()
    );
    if !prefix.is_empty() {
        println!("names relative to {prefix}");
    }

    // Coverage gaps come first so a sparse graph is never read as clean.
    let mut notes: Vec<(String, Vec<String>)> = vec![];
    if !graph.unattributed.is_empty() {
        notes.push((
            format!(
                "{} file(s) import but define no symbols; their module is unknown and their imports are not in the graph",
                graph.unattributed.len()
            ),
            graph.unattributed.clone(),
        ));
    }
    if !unresolved_total.is_empty() {
        notes.push((
            format!(
                "{} import source(s) name a project namespace with no indexed module",
                unresolved_total.len()
            ),
            unresolved_total
                .iter()
                .map(|s| {
                    if s.is_empty() {
                        "(empty)".to_owned()
                    } else {
                        (*s).clone()
                    }
                })
                .collect(),
        ));
    }
    if !lookalikes.is_empty() {
        notes.push((
            format!(
                "{} external source(s) share a name with a project module; relative or short imports are not resolved",
                lookalikes.len()
            ),
            lookalikes
                .iter()
                .map(|(s, hits)| {
                    let names: Vec<String> = hits.iter().map(|&i| short(i)).collect();
                    format!("{s} ~ {}", names.join(", "))
                })
                .collect(),
        ));
    }
    for (summary, items) in &notes {
        println!("note: {summary}");
        if opts.verbose {
            for item in items {
                println!("      {item}");
            }
        }
    }
    if !opts.verbose && !notes.is_empty() {
        println!("      (-v lists them)");
    }

    println!("\ncycles ({})", cycles.len());
    if cycles.is_empty() {
        println!("  none");
    }
    // Sites are always shown: they tell a real cycle from one that only
    // exists through files the user may want to --exclude (e.g. tests).
    let site_cap = if opts.verbose { 0 } else { 2 };
    for (members, path) in capped(&cycles, opts.limit) {
        let arrow: Vec<String> = path.iter().map(|&i| short(i)).collect();
        println!("  {}  [{} modules]", arrow.join(" → "), members.len());
        for w in path.windows(2) {
            let sites: Vec<&ImportSite> = graph.imports[w[0]][&w[1]].iter().collect();
            let shown: Vec<String> = capped(&sites, site_cap)
                .iter()
                .map(|s| format!("{}:{}", s.file, s.line))
                .collect();
            let more = sites.len() - shown.len();
            let more = if more > 0 {
                format!(" (+{more} more)")
            } else {
                String::new()
            };
            println!(
                "      {} → {}  {}{more}",
                short(w[0]),
                short(w[1]),
                shown.join(", ")
            );
        }
    }

    println!("\nhotspots — most imported");
    for &i in capped(&by_fan_in, opts.limit) {
        println!("  {:>5}  {}", fan_in[i], short(i));
    }
    println!("\nfragile — most imports");
    for &i in capped(&by_fan_out, opts.limit) {
        if opts.verbose {
            println!(
                "  {:>5}  {}  (+{} external)",
                graph.imports[i].len(),
                short(i),
                graph.external[i].len()
            );
        } else {
            println!("  {:>5}  {}", graph.imports[i].len(), short(i));
        }
    }
    println!("\ndeep chains — longest import paths");
    if chains.is_empty() {
        println!("  none");
    }
    for chain in &chains {
        let names: Vec<String> = chain.iter().map(|&i| short(i)).collect();
        println!("  {:>5}  {}", chain.len(), names.join(" → "));
    }
    true
}

fn print_closure(
    idx: &Index,
    graph: &ModuleGraph,
    query: &str,
    reverse: bool,
    opts: &MapOptions,
    short: &dyn Fn(usize) -> String,
) -> bool {
    let target = match graph.find(idx, query) {
        Ok(t) => t,
        Err(candidates) => {
            let code = if candidates.is_empty() {
                "module_not_found"
            } else {
                "ambiguous_module"
            };
            if opts.json {
                println!(
                    "{}",
                    json!({"status": "error", "error_code": code, "query": query, "candidates": candidates})
                );
            } else if candidates.is_empty() {
                eprintln!("error [{code}]: no module matches {query:?}");
            } else {
                eprintln!("error [{code}]: {query:?} matches several modules:");
                for c in &candidates {
                    eprintln!("  {c}");
                }
            }
            return false;
        }
    };
    let reached = graph.closure(target, reverse, opts.depth);
    let direction = if reverse { "dependents" } else { "deps" };
    if opts.json {
        let out = json!({
            "target": graph.modules[target],
            "direction": direction,
            "total": reached.len(),
            "modules": reached.iter().map(|&(m, depth, via)| json!({
                "module": graph.modules[m], "depth": depth, "via": graph.modules[via],
            })).collect::<Vec<_>>(),
        });
        println!("{}", serde_json::to_string_pretty(&out).unwrap_or_default());
        return true;
    }
    if reverse {
        println!("{}: {} dependent(s)", graph.modules[target], reached.len());
    } else {
        println!("{}: depends on {}", graph.modules[target], reached.len());
    }
    let mut current = 0;
    for &(m, depth, via) in &reached {
        if depth != current {
            current = depth;
            let count = reached.iter().filter(|r| r.1 == depth).count();
            let label = if depth == 1 { "direct" } else { "transitive" };
            println!("\ndepth {depth} — {label} ({count})");
        }
        if opts.verbose && depth > 1 {
            println!("  {}  via {}", short(m), short(via));
        } else {
            println!("  {}", short(m));
        }
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    fn intern(idx: &mut Index, s: &str) -> i64 {
        if let Some(i) = idx.strings.iter().position(|x| x == s) {
            return i as i64;
        }
        idx.strings.push(s.to_owned());
        idx.strings.len() as i64 - 1
    }

    /// (path, top-level symbol qpaths, (local, source) imports)
    type TestFile<'a> = (&'a str, &'a [&'a str], &'a [(&'a str, &'a str)]);

    fn index(files: &[TestFile]) -> Index {
        let mut idx = Index::empty(std::path::Path::new("/nonexistent"));
        for (fid, (rel, symbols, imports)) in files.iter().enumerate() {
            let fid = fid as i64;
            idx.files.push((*rel).to_owned());
            idx.file_langs.push(0);
            for qpath in *symbols {
                let row = idx.symbols.len() as i64;
                let path_id = intern(&mut idx, qpath);
                idx.symbols.push([path_id, fid, 1, 1, 0, 1, 0, 0, -1]);
                idx.by_path
                    .entry((*qpath).to_owned())
                    .or_default()
                    .push(row);
                idx.by_file.entry(fid).or_default().push(row);
            }
            for (local, source) in *imports {
                let imp = idx.imports.len() as i64;
                let name_id = intern(&mut idx, local);
                let source_id = intern(&mut idx, source);
                idx.imports.push([fid, name_id, source_id, 1]);
                idx.imps_by_file.entry(fid).or_default().push(imp);
            }
        }
        idx
    }

    fn names(g: &ModuleGraph, ids: &[usize]) -> Vec<String> {
        ids.iter().map(|&i| g.modules[i].clone()).collect()
    }

    fn id(g: &ModuleGraph, m: &str) -> usize {
        g.modules.iter().position(|x| x == m).unwrap()
    }

    #[test]
    fn go_module_from_functions_and_methods() {
        let idx = index(&[
            ("a/types.go", &["ex.com/m/a.T"], &[]),
            (
                "a/methods.go",
                &["ex.com/m/a.T.Get", "ex.com/m/a.T.Set"],
                &[],
            ),
            ("gopkg/yaml.go", &["gopkg.in/yaml.v3.Marshal"], &[]),
        ]);
        assert_eq!(file_module(&idx, 0).as_deref(), Some("ex.com/m/a"));
        assert_eq!(file_module(&idx, 1).as_deref(), Some("ex.com/m/a"));
        assert_eq!(file_module(&idx, 2).as_deref(), Some("gopkg.in/yaml.v3"));
    }

    #[test]
    fn python_from_import_targets_submodule_or_module() {
        let idx = index(&[
            (
                "pkg/a.py",
                &["pkg.a.f"],
                &[("b", "pkg"), ("helper", "pkg.c"), ("os", "os")],
            ),
            ("pkg/b.py", &["pkg.b.g"], &[]),
            ("pkg/c.py", &["pkg.c.helper"], &[]),
        ]);
        let g = ModuleGraph::from_index(&idx, &[]);
        let a = id(&g, "pkg.a");
        let targets: Vec<usize> = g.imports[a].keys().copied().collect();
        assert_eq!(names(&g, &targets), ["pkg.b", "pkg.c"]);
        assert_eq!(g.external[a].iter().collect::<Vec<_>>(), ["os"]);
    }

    #[test]
    fn short_external_sources_matching_modules_are_flagged() {
        // `from .b import g` reaches the index as source "b".
        let idx = index(&[
            ("pkg/a.py", &["pkg.a.f"], &[("g", "b"), ("os", "os")]),
            ("pkg/b.py", &["pkg.b.g"], &[]),
        ]);
        let g = ModuleGraph::from_index(&idx, &[]);
        let flagged = g.external_matching_modules();
        assert_eq!(flagged.len(), 1);
        assert_eq!(flagged[0].0, "b");
        assert_eq!(names(&g, &flagged[0].1), ["pkg.b"]);
    }

    #[test]
    fn symbol_less_files_are_unattributed_and_namespaces_unresolved() {
        let idx = index(&[
            ("pkg/__init__.py", &[], &[("f", "pkg.a")]),
            ("pkg/a.py", &["pkg.a.f"], &[]),
            ("app.py", &["app.main"], &[("f", "pkg"), ("x", "")]),
        ]);
        let g = ModuleGraph::from_index(&idx, &[]);
        assert_eq!(g.unattributed, ["pkg/__init__.py"]);
        let app = id(&g, "app");
        assert!(g.imports[app].is_empty());
        assert_eq!(g.unresolved[app].iter().collect::<Vec<_>>(), ["", "pkg"]);
        assert!(g.external[app].is_empty());
    }

    #[test]
    fn detects_cycles_as_components() {
        let idx = index(&[
            ("a.go", &["m/a.A"], &[("b", "m/b")]),
            ("b.go", &["m/b.B"], &[("c", "m/c")]),
            ("c.go", &["m/c.C"], &[("a", "m/a"), ("d", "m/d")]),
            ("d.go", &["m/d.D"], &[]),
            ("e.go", &["m/e.E"], &[("d", "m/d")]),
        ]);
        let g = ModuleGraph::from_index(&idx, &[]);
        let cycles = g.cycles();
        assert_eq!(cycles.len(), 1);
        assert_eq!(names(&g, &cycles[0].0), ["m/a", "m/b", "m/c"]);
        assert_eq!(names(&g, &cycles[0].1), ["m/a", "m/b", "m/c", "m/a"]);
    }

    #[test]
    fn no_cycles_in_a_dag() {
        let idx = index(&[
            ("a.go", &["m/a.A"], &[("b", "m/b"), ("c", "m/c")]),
            ("b.go", &["m/b.B"], &[("c", "m/c")]),
            ("c.go", &["m/c.C"], &[]),
        ]);
        let g = ModuleGraph::from_index(&idx, &[]);
        assert!(g.cycles().is_empty());
        assert_eq!(names(&g, &g.deep_chains(0)[0]), ["m/a", "m/b", "m/c"]);
    }

    #[test]
    fn dependents_and_deps_closures_carry_depth() {
        let idx = index(&[
            ("cmd/main.go", &["m/cmd.main"], &[("api", "m/api")]),
            (
                "api/api.go",
                &["m/api.Serve"],
                &[("client", "m/gateway/client")],
            ),
            (
                "gateway/client/c.go",
                &["m/gateway/client.New"],
                &[("util", "m/util")],
            ),
            ("util/u.go", &["m/util.X"], &[]),
            ("other/o.go", &["m/other.O"], &[]),
        ]);
        let g = ModuleGraph::from_index(&idx, &[]);
        let client = g.find(&idx, "gateway/client").unwrap();
        let up: Vec<(String, usize)> = g
            .closure(client, true, None)
            .into_iter()
            .map(|(m, d, _)| (g.modules[m].clone(), d))
            .collect();
        assert_eq!(up, [("m/api".to_owned(), 1), ("m/cmd".to_owned(), 2)]);
        assert_eq!(g.closure(client, true, Some(1)).len(), 1);
        let cmd = g.find(&idx, "m/cmd").unwrap();
        let down: Vec<String> = g
            .closure(cmd, false, None)
            .into_iter()
            .map(|(m, _, _)| g.modules[m].clone())
            .collect();
        assert_eq!(down, ["m/api", "m/gateway/client", "m/util"]);
        assert_eq!(g.find(&idx, "gateway/client/c.go"), Ok(client));
    }

    #[test]
    fn find_reports_ambiguity_and_absence() {
        let idx = index(&[
            ("a/client/c.go", &["m/a/client.C"], &[]),
            ("b/client/c.go", &["m/b/client.C"], &[]),
        ]);
        let g = ModuleGraph::from_index(&idx, &[]);
        assert_eq!(g.find(&idx, "client").unwrap_err().len(), 2);
        assert!(g.find(&idx, "nope").unwrap_err().is_empty());
        assert!(g.find(&idx, "ient").unwrap_err().is_empty());
    }

    #[test]
    fn cross_module_imports_are_internal_edges() {
        // Two modules of one workspace: m and m/sdk.
        let idx = index(&[
            (
                "app/app.go",
                &["ex.com/m/app.Run"],
                &[("client", "ex.com/m/sdk/client")],
            ),
            (
                "sdk/client/c.go",
                &["ex.com/m/sdk/client.New"],
                &[("http", "net/http")],
            ),
        ]);
        let g = ModuleGraph::from_index(&idx, &[]);
        let app = id(&g, "ex.com/m/app");
        let client = id(&g, "ex.com/m/sdk/client");
        assert!(g.imports[app].contains_key(&client));
        assert_eq!(g.external[client].iter().collect::<Vec<_>>(), ["net/http"]);
        assert_eq!(g.find(&idx, "sdk/client"), Ok(client));
    }

    #[test]
    fn excluded_files_drop_their_imports() {
        let idx = index(&[
            ("a/a.go", &["m/a.A"], &[]),
            ("a/a_test.go", &["m/a.TestA"], &[("b", "m/b")]),
            ("b/b.go", &["m/b.B"], &[("a", "m/a")]),
        ]);
        let all = ModuleGraph::from_index(&idx, &[]);
        assert_eq!(all.cycles().len(), 1);
        let pat = GlobPat::new("*_test.go").unwrap();
        let prod = ModuleGraph::from_index(&idx, &[pat]);
        assert!(prod.cycles().is_empty());
        assert_eq!(prod.edge_count(), 1);
    }

    #[test]
    fn shared_prefix_cuts_at_boundary() {
        let mods = ["github.com/o/obot/pkg/a", "github.com/o/obot/pkg/api"].map(String::from);
        assert_eq!(shared_prefix(&mods), "github.com/o/obot/pkg/");
        let mods = ["flask.app", "flask.cli"].map(String::from);
        assert_eq!(shared_prefix(&mods), "flask.");
        let mods = ["ex.com/m", "ex.com/m/a", "ex.com/m/b"].map(String::from);
        assert_eq!(shared_prefix(&mods), "ex.com/m/");
        let mods = ["app", "tests.conftest"].map(String::from);
        assert_eq!(shared_prefix(&mods), "");
    }
}
