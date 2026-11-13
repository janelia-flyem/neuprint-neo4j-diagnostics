#!/usr/bin/env Rscript
#
# check-neuprintr.R [server] [dataset | server/dataset ...] [--neuprintr-path DIR] [--color WHEN]
#
# Run the REAL neuprintr functions against live servers and check that the
# Cypher they emit still executes.
#
#     ./check-neuprintr.R neuprint-pre wasp3:v0.0
#     ./check-neuprintr.R neuprint                     # every visible dataset
#     ./check-neuprintr.R neuprint-pre wasp3:v0.0 --neuprintr-path ~/git/neuprintr
#
#     # one run spanning the fleet: a bare dataset uses the first server named,
#     # a server/dataset pair names its own (datasets contain ':', never '/')
#     ./check-neuprintr.R neuprint hemibrain:v1.2.1 hemibrain:v1.2.3 neuprint-pre/wasp3:v0.0
#
# A removed construct only shows up as a *difference* when the run spans more
# than one Neo4j generation, so the roll-up names the generations it covered
# and warns when there was only one. (The emitted-text scan below does not
# have that limitation; it flags the syntax on any server.)
#
# Why this exists
# ---------------
# neuprintr's own test suite is pinned to one dataset (hemibrain body ids,
# hemibrain cell types, a saved ROI hierarchy), so pointed at any other
# dataset it fails for data reasons and says nothing about the Cypher. This
# script discovers a body, a type and an ROI on each dataset it is given, then
# calls the package functions whose queries changed for Neo4j 5 -- the ones
# that used `exists()` and `apoc.meta.type()`, both removed in 5.0 -- and
# reports per function and per dataset. Run against several datasets on
# different Neo4j generations it produces the compatibility table directly.
#
# It is the R counterpart of check-neuprint-python.py, and looks for the same
# two things:
#
#   * **Emitted text.** Every Cypher statement the package sends is captured
#     (by tracing neuprint_fetch_custom) and scanned for `exists(` and
#     `apoc.meta.type(`. That needs no migrated dataset to detect: it would
#     flag the defect against a fleet that is still entirely 4.4.
#   * **Execution.** Each function is called with real arguments and must
#     return a sensible value. A SyntaxError on one Neo4j generation and not
#     another is the signature of a removed construct.
#
# A function that runs but finds nothing (no synapses in the sampled ROI, say)
# is reported NO-DATA, not OK: "it did not fail" is trivially true when
# nothing was asked of it. NO-DATA rows become warnings in the roll-up.
#
# Requirements
# ------------
# Unlike almost everything else in this repo, this needs R with neuprintr and
# its dependencies installed, because the point is to exercise the real
# package rather than a transcription of its queries. Either `library(neuprintr)`
# must work, or pass `--neuprintr-path <checkout>` and have pkgload available
# (it ships with devtools); the checkout is then loaded in place, so a branch
# can be tested without installing it. Without a loadable package the script
# exits 3 and says so, rather than silently testing nothing.
#
# Auth: NEUPRINT_JWT or NEUPRINT_TOKEN. Sent in a request header only -- never
# printed, written to the report, or placed on a command line.
#
# Read-only. Exit 0 if every function ran and emitted clean Cypher on every
# dataset, 1 if any did not, 2 on a bad invocation, 3 if neuprintr could not
# be loaded.

# ---- arguments --------------------------------------------------------------

usage <- function(status = 2) {
  cat("usage: check-neuprintr.R [server] [dataset | server/dataset ...] [--neuprintr-path DIR] [--color auto|always|never]\n",
      file = stderr())
  quit(status = status)
}

args <- commandArgs(trailingOnly = TRUE)
opt <- list(path = NULL, color = NULL)
positional <- character()
i <- 1
while (i <= length(args)) {
  a <- args[i]
  if (a == "--neuprintr-path") {
    if (i == length(args)) usage()
    opt$path <- args[i + 1]; i <- i + 2
  } else if (a == "--color") {
    if (i == length(args) || !args[i + 1] %in% c("auto", "always", "never")) usage()
    opt$color <- args[i + 1]; i <- i + 2
  } else if (a %in% c("-h", "--help")) {
    usage(0)
  } else if (startsWith(a, "--")) {
    usage()
  } else {
    positional <- c(positional, a); i <- i + 1
  }
}
if (length(positional) < 1) usage()

full_url <- function(s) {
  if (grepl("^https?://", s)) s
  else if (grepl(".", s, fixed = TRUE)) paste0("https://", s)
  else paste0("https://", s, ".janelia.org")
}
short_name <- function(s) sub("^https?://", "", sub("\\.janelia\\.org$", "", s))

# Positional arguments are one of: a bare server (the first such one is the
# default), a bare dataset (uses the default server), or server/dataset.
# Datasets contain ':' (hemibrain:v1.2.1) but never '/', so the slash is
# unambiguous; the split is at the LAST slash so the server part may be a URL.
default_server <- NULL
targets <- list()                          # list(server=, dataset=)
for (a in positional) {
  if (grepl("/", a, fixed = TRUE) && !grepl("^https?://[^/]+$", a)) {
    cut <- regexpr("/[^/]*$", a)
    targets[[length(targets) + 1]] <- list(server = full_url(substr(a, 1, cut - 1)),
                                           dataset = substr(a, cut + 1, nchar(a)))
  } else if (is.null(default_server) && length(targets) == 0 && !grepl(":", a, fixed = TRUE)) {
    default_server <- full_url(a)
  } else {
    if (is.null(default_server)) {
      cat(sprintf("error: dataset %s needs a server; name one first or write server/%s\n", a, a), file = stderr())
      usage()
    }
    targets[[length(targets) + 1]] <- list(server = default_server, dataset = a)
  }
}
if (is.null(default_server) && length(targets) == 0) usage()

# ---- color, matching common/style.py --------------------------------------
# red: a failure. yellow: a warning. green: as expected. cyan: a heading.
# dim: an aside. Color goes at the END of a formatted field, never inside a
# width, and the saved report is always plain text.

want_color <- function(choice) {
  if (!is.null(choice) && choice != "auto") return(choice == "always")
  if (is.null(choice)) {
    env <- tolower(trimws(Sys.getenv("COLOR", "")))
    if (env %in% c("always", "never")) return(env == "always")
    if (!is.na(Sys.getenv("NO_COLOR", NA))) return(FALSE)
  }
  isatty(stdout())
}
COLOR_ON <- want_color(opt$color)
paint <- function(code, x) if (COLOR_ON) sprintf("\033[%sm%s\033[0m", code, x) else x
red    <- function(x) paint("31;1", x)
yellow <- function(x) paint("33;1", x)
green  <- function(x) paint("32", x)
cyan   <- function(x) paint("36", x)
dim    <- function(x) paint("2", x)
strip_ansi <- function(x) gsub("\033\\[[0-9;]*m", "", x)

report_lines <- character()
findings <- list()      # list(severity, text)
say <- function(...) {
  line <- paste0(...)
  cat(line, "\n", sep = "")
  report_lines <<- c(report_lines, strip_ansi(line))
}
heading <- function(x) { say(""); say(cyan(x)) }
finding <- function(severity, text) findings[[length(findings) + 1]] <<- list(severity, text)

# ---- token ------------------------------------------------------------------

token <- Sys.getenv("NEUPRINT_JWT", "")
if (!nzchar(token)) token <- Sys.getenv("NEUPRINT_TOKEN", "")
if (!nzchar(token)) {
  cat("ERROR: set NEUPRINT_JWT (or NEUPRINT_TOKEN).\n", file = stderr())
  quit(status = 2)
}

# ---- load neuprintr ---------------------------------------------------------

load_neuprintr <- function(path) {
  if (!is.null(path)) {
    if (!requireNamespace("pkgload", quietly = TRUE))
      return("pkgload is not installed, so --neuprintr-path cannot be used")
    r <- try(suppressMessages(pkgload::load_all(path.expand(path), quiet = TRUE,
                                                 export_all = FALSE)), silent = TRUE)
    if (inherits(r, "try-error")) return(conditionMessage(attr(r, "condition")))
  } else {
    r <- try(suppressPackageStartupMessages(library(neuprintr)), silent = TRUE)
    if (inherits(r, "try-error")) return(conditionMessage(attr(r, "condition")))
  }
  NULL
}

err <- load_neuprintr(opt$path)
if (!is.null(err)) {
  say(red("ERROR: neuprintr could not be loaded -- nothing was tested."))
  say("  ", err)
  say("")
  say("  This check needs the real neuprintr package, since the point is to")
  say("  exercise its queries rather than a copy. Either install it, or pass")
  say("  --neuprintr-path <checkout> (needs pkgload).")
  quit(status = 3)
}
ns <- asNamespace("neuprintr")
np_version <- as.character(utils::packageVersion("neuprintr"))
np_where <- if (!is.null(opt$path)) normalizePath(path.expand(opt$path)) else dirname(system.file(package = "neuprintr"))

# ---- capture every Cypher statement the package emits ----------------------
# trace() replaces neuprint_fetch_custom in the namespace with a version that
# first evaluates the tracer in the function's own frame, where `cypher` is
# the argument. Package-internal calls, including the `neuprintr::` form that
# neuprint_typeof uses, all resolve to the traced copy.

.captured <- new.env()
.captured$cypher <- character()
.captured$on <- TRUE
.np_record <- function(q) if (isTRUE(.captured$on)) .captured$cypher <- c(.captured$cypher, as.character(q))
# trace() announces itself with both a message and a print(); silence both.
invisible(capture.output(suppressMessages(
  trace("neuprint_fetch_custom", where = ns, print = FALSE, tracer = quote(.np_record(cypher))))))
record <- function(on) .captured$on <- on
quiet_fetch <- function(cypher, conn, dataset) {
  # a harness query, not one the package chose: do not scan it
  record(FALSE); on.exit(record(TRUE))
  neuprintr::neuprint_fetch_custom(cypher, conn = conn, dataset = dataset)
}

# ---- helpers ----------------------------------------------------------------

neo4j_version <- function(conn, ds) {
  r <- try(quiet_fetch("CALL dbms.components() YIELD versions RETURN versions[0]", conn, ds), silent = TRUE)
  if (inherits(r, "try-error") || length(r$data) == 0) "unknown" else as.character(r$data[[1]][[1]])
}

# one call = one row of the table. fn returns one of:
#   list(ok = TRUE,  note = "...")   OK
#   list(ok = NA,    note = "...")   NO-DATA (ran, nothing to judge)
#   list(ok = FALSE, note = "...")   FAIL (returned the wrong thing)
# and an R error is a FAIL with the condition message.
run_check <- function(ds, label, fn) {
  before <- length(.captured$cypher)
  res <- tryCatch(suppressWarnings(fn()), error = function(e) {
    msg <- conditionMessage(e)
    msg <- sub(".*neuPrint error: ", "", msg)
    # keep the part of a Neo4j error that says what was wrong, not the wrapper
    msg <- sub(".*Neo\\.ClientError\\.[A-Za-z.]+ \\((.*)", "\\1", msg)
    msg <- gsub("\\s+", " ", msg)
    list(ok = FALSE, note = substr(msg, 1, 90))
  })
  # not x[-seq_len(before)]: with before == 0 that is x[integer(0)], i.e. nothing
  n_sent <- length(.captured$cypher) - before
  verdict <- if (isTRUE(res$ok)) green("OK     ") else if (is.na(res$ok)) yellow("NO-DATA") else red("FAIL   ")
  say(sprintf("  %-34s %s  %s", label, verdict,
              dim(sprintf("%s  [%d quer%s]", res$note, n_sent, if (n_sent == 1) "y" else "ies"))))
  if (isTRUE(res$ok)) return(TRUE)
  if (is.na(res$ok)) { finding("warn", sprintf("%s / %s: not exercised (%s)", ds, label, res$note)); return(TRUE) }
  finding("fail", sprintf("%s / %s: %s", ds, label, res$note))
  FALSE
}

fmt_n <- function(n) format(n, big.mark = ",")
count_of <- function(n, noun) sprintf("%s %s%s", fmt_n(n), noun, if (n == 1) "" else "s")

# ---- per-dataset discovery and checks ---------------------------------------

# Returns list(ok = <logical>, version = <neo4j version or NA>).
check_dataset <- function(server, ds) {
  conn <- tryCatch(
    suppressWarnings(neuprintr::neuprint_login(server = server, token = token, dataset = ds,
                                               Cache = FALSE, Force = TRUE)),
    error = function(e) e)
  ver <- if (inherits(conn, "error")) "unreachable" else neo4j_version(conn, ds)
  heading(sprintf("=== %s/%s   (neo4j %s)", short_name(server), ds, ver))
  if (inherits(conn, "error")) {
    say("  ", yellow("skipped"), " -- could not log in: ", conditionMessage(conn))
    finding("warn", sprintf("%s: not reachable", ds))
    return(list(ok = TRUE, version = NA_character_))
  }
  version <- if (ver %in% c("unknown", "unreachable")) NA_character_ else ver
  skipped <- function() list(ok = TRUE, version = version)

  # Discover arguments on this dataset rather than hard-coding hemibrain's.
  pair <- try(quiet_fetch("MATCH (a:Neuron)-[c:ConnectsTo]->(b:Neuron) WHERE c.weight >= 5 RETURN a.bodyId, b.bodyId LIMIT 1", conn, ds), silent = TRUE)
  if (inherits(pair, "try-error") || length(pair$data) == 0) {
    say("  ", yellow("skipped"), " -- no connected Neuron pair found to test with")
    finding("warn", sprintf("%s: no Neuron pair found", ds))
    return(skipped())
  }
  body <- as.character(pair$data[[1]][[1]])
  typ <- try(quiet_fetch("MATCH (n:Neuron) WHERE n.type IS NOT NULL RETURN n.type LIMIT 1", conn, ds), silent = TRUE)
  type_value <- if (inherits(typ, "try-error") || length(typ$data) == 0) NA_character_ else as.character(typ$data[[1]][[1]])

  # An ROI the sampled body actually innervates, so the roi= query has rows.
  rois <- try(suppressWarnings(neuprintr::neuprint_ROIs(superLevel = NULL, dataset = ds, conn = conn)), silent = TRUE)
  roi_info <- try(quiet_fetch(sprintf("MATCH (n:Neuron {bodyId: %s}) RETURN n.roiInfo", body), conn, ds), silent = TRUE)
  roi <- NA_character_
  if (!inherits(rois, "try-error") && !inherits(roi_info, "try-error") && length(roi_info$data) &&
      !is.null(roi_info$data[[1]][[1]])) {
    innervated <- names(jsonlite::fromJSON(roi_info$data[[1]][[1]]))
    hit <- intersect(innervated, rois)
    if (length(hit)) roi <- hit[1]
  }
  say(dim(sprintf("  sampled: bodyId %s, type %s, roi %s", body,
                  if (is.na(type_value)) "<none>" else shQuote(type_value),
                  if (is.na(roi)) "<none>" else shQuote(roi))))

  ok <- TRUE
  ok <- run_check(ds, "neuprint_typeof(bodyId, neo4j)", function() {
    v <- neuprintr:::neuprint_typeof("bodyId", type = "neo4j", conn = conn, dataset = ds, cache = FALSE)
    if (identical(v, "INTEGER")) list(ok = TRUE, note = v) else list(ok = FALSE, note = paste("got", deparse(v)))
  }) && ok
  ok <- run_check(ds, "neuprint_typeof(bodyId, r)", function() {
    v <- neuprintr:::neuprint_typeof("bodyId", type = "r", conn = conn, dataset = ds, cache = FALSE)
    if (identical(v, "numeric")) list(ok = TRUE, note = v) else list(ok = FALSE, note = paste("got", deparse(v)))
  }) && ok
  ok <- run_check(ds, "neuprint_typeof(type, neo4j)", function() {
    if (is.na(type_value)) return(list(ok = NA, note = "no Neuron has a type"))
    v <- neuprintr:::neuprint_typeof("type", type = "neo4j", conn = conn, dataset = ds, cache = FALSE)
    if (identical(v, "STRING")) list(ok = TRUE, note = v) else list(ok = FALSE, note = paste("got", deparse(v)))
  }) && ok
  ok <- run_check(ds, "neuprint_name_field()", function() {
    v <- neuprintr:::neuprint_name_field(conn = conn, dataset = ds)
    if (v %in% c("instance", "name")) list(ok = TRUE, note = v) else list(ok = FALSE, note = paste("got", deparse(v)))
  }) && ok
  ok <- run_check(ds, "neuprint_search(type, fixed, exact)", function() {
    if (is.na(type_value)) return(list(ok = NA, note = "no Neuron has a type"))
    v <- neuprintr::neuprint_search(type_value, field = "type", fixed = TRUE, exact = TRUE, meta = FALSE,
                                    conn = conn, dataset = ds)
    if (length(v) >= 1) list(ok = TRUE, note = count_of(length(v), "id")) else list(ok = FALSE, note = "0 ids for a value that exists")
  }) && ok
  ok <- run_check(ds, "neuprint_search(where: ...)", function() {
    v <- neuprintr::neuprint_search(sprintf("where:n.bodyId = %s", body), meta = FALSE, conn = conn, dataset = ds)
    if (identical(as.character(v), body)) list(ok = TRUE, note = "1 id, as expected") else list(ok = FALSE, note = paste("got", deparse(v)))
  }) && ok
  ok <- run_check(ds, "neuprint_ids(!type)", function() {
    if (is.na(type_value)) return(list(ok = NA, note = "no Neuron has a type"))
    v <- neuprintr::neuprint_ids(paste0("!", type_value), conn = conn, dataset = ds)
    if (length(v) >= 1) list(ok = TRUE, note = count_of(length(v), "id")) else list(ok = FALSE, note = "0 ids")
  }) && ok
  ok <- run_check(ds, "neuprint_get_meta(body)", function() {
    m <- neuprintr::neuprint_get_meta(body, conn = conn, dataset = ds)
    if (!is.data.frame(m) || nrow(m) != 1) return(list(ok = FALSE, note = sprintf("%d rows", NROW(m))))
    if (!"soma" %in% names(m) || !is.logical(m$soma) || is.na(m$soma)) return(list(ok = FALSE, note = "soma column missing or NA"))
    list(ok = TRUE, note = sprintf("%d cols, soma=%s", ncol(m), m$soma))
  }) && ok
  ok <- run_check(ds, "neuprint_get_synapses(body, roi)", function() {
    if (is.na(roi)) return(list(ok = NA, note = "no ROI to filter on"))
    s <- neuprintr::neuprint_get_synapses(body, roi = roi, conn = conn, dataset = ds)
    if (!is.data.frame(s)) return(list(ok = FALSE, note = "not a data.frame"))
    if (nrow(s) == 0) list(ok = NA, note = "0 synapses in sampled ROI") else list(ok = TRUE, note = count_of(nrow(s), "synapse"))
  }) && ok
  ok <- run_check(ds, "neuprint_connection_table(details)", function() {
    d <- neuprintr::neuprint_connection_table(body, prepost = "POST", details = TRUE, conn = conn, dataset = ds)
    if (!is.data.frame(d)) return(list(ok = FALSE, note = "not a data.frame"))
    if (nrow(d) == 0) list(ok = NA, note = "0 partners") else list(ok = TRUE, note = count_of(nrow(d), "partner"))
  }) && ok
  list(ok = ok, version = version)
}

# ---- run --------------------------------------------------------------------

say(sprintf("neuprintr:  %s  (%s)", np_version, np_where))

if (length(targets) == 0) {
  # a server and nothing else: every dataset it shows this token
  conn0 <- tryCatch(suppressWarnings(neuprintr::neuprint_login(server = default_server, token = token,
                                                               Cache = FALSE, Force = TRUE)),
                    error = function(e) e)
  if (inherits(conn0, "error")) {
    say(red("ERROR: could not log in: "), conditionMessage(conn0))
    quit(status = 2)
  }
  for (ds in sort(names(neuprintr::neuprint_datasets(conn = conn0))))
    targets[[length(targets) + 1]] <- list(server = default_server, dataset = ds)
}
if (length(targets) == 0) {
  say(red("ERROR: no datasets visible. Check the token."))
  quit(status = 2)
}
servers <- unique(vapply(targets, `[[`, "", "server"))
say(sprintf("server%s:    %s", if (length(servers) == 1) " " else "s", paste(servers, collapse = ", ")))

# neuprint_typeof() wraps its query in try(), which prints the raw server
# error to stderr before returning NA. The FAIL row carries that message, so
# the duplicate would only clutter the report.
old_opts <- options(show.error.messages = FALSE)
exec_ok <- TRUE
versions <- character()
for (t in targets) {
  r <- check_dataset(t$server, t$dataset)
  exec_ok <- r$ok && exec_ok
  versions <- c(versions, r$version)
}
options(old_opts)

# ---- the emitted text -------------------------------------------------------

heading("--- Cypher the package emitted ---")
sent <- unique(.captured$cypher)
say(dim(sprintf("  %d distinct statements captured from neuprint_fetch_custom", length(sent))))
say(dim("  Any exists( ) or apoc.meta.type( ) here is syntax Neo4j 5 removed, whatever a 4.4 server accepts."))
text_ok <- TRUE
for (pat in c("exists(", "apoc.meta.type(")) {
  hits <- sent[grepl(pat, sent, fixed = TRUE)]
  if (length(hits)) {
    text_ok <- FALSE
    say(sprintf("  %-18s %s  %s", pat, red("EMITTED"), dim(sprintf("in %d statement%s, e.g.", length(hits), if (length(hits) == 1) "" else "s"))))
    say(dim(paste0("      ", substr(gsub("\\s+", " ", hits[1]), 1, 110))))
    finding("fail", sprintf("emitted Cypher: %s appears in %d statement(s)", pat, length(hits)))
  } else {
    say(sprintf("  %-18s %s", pat, green("absent")))
  }
}

# ---- roll-up ----------------------------------------------------------------

fails <- Filter(function(f) f[[1]] == "fail", findings)
warns <- Filter(function(f) f[[1]] == "warn", findings)
if (length(findings)) {
  heading("--- findings ---")
  for (f in fails) say("  ", red("FAIL  "), f[[2]])
  for (f in warns) say("  ", yellow("WARN  "), f[[2]])
}
say("")
# Which Neo4j generations did this run actually touch? A construct that one
# generation removed can only appear as a difference if another was in the
# run; with a single generation the execution half of this check proves less
# than its green rows suggest, and the roll-up should say so.
covered <- unique(versions[!is.na(versions)])
# numeric order, so 3.5.3 < 4.4.16 < 2026.08.1 rather than the string order
covered <- covered[order(numeric_version(covered, strict = FALSE), covered)]
if (length(covered) == 0) {
  say("neo4j generations covered: ", yellow("none"), dim("  (no dataset answered dbms.components())"))
} else {
  say(sprintf("neo4j generations covered: %s  %s", paste(covered, collapse = ", "),
              dim(sprintf("(%d dataset%s)", length(versions), if (length(versions) == 1) "" else "s"))))
  if (length(covered) == 1) {
    say("  ", yellow("one generation only"),
        " -- a construct removed in another cannot show up as a difference here;")
    say("  only the emitted-text scan above speaks to that. Add a dataset on another server,")
    say("  e.g. ", dim("neuprint/hemibrain:v1.2.3 neuprint-pre/wasp3:v0.0"), ".")
    finding("warn", sprintf("coverage: one Neo4j generation only (%s)", covered))
    warns <- Filter(function(f) f[[1]] == "warn", findings)
  }
}
say("")
ok <- exec_ok && text_ok
if (ok) {
  say("RESULT: ", green("OK"), sprintf(" -- every function ran and emitted clean Cypher on %d dataset%s.",
                                     length(targets), if (length(targets) == 1) "" else "s"))
  if (length(warns)) say(dim("        (see WARN lines: NO-DATA rows were not exercised, or coverage was thin.)"))
} else {
  say("RESULT: ", red("FAILED"), " -- see findings above. An EMITTED line means this neuprintr")
  say("will break against any Neo4j 5 or CalVer dataset; a FAIL line is one that already did.")
}

# ---- save the plain-text report ---------------------------------------------

repo_root <- normalizePath(file.path(dirname(sub("--file=", "", grep("--file=", commandArgs(), value = TRUE)[1])), ".."))
out_dir <- file.path(repo_root, "results")
dir.create(out_dir, showWarnings = FALSE)
stamp <- format(Sys.time(), "%Y%m%d-%H%M%S")
out <- file.path(out_dir, sprintf("neuprintr-%s-%s.txt", paste(short_name(servers), collapse = "+"), stamp))
writeLines(report_lines, out)
say(dim(sprintf("report: %s", out)))

invisible(capture.output(suppressMessages(untrace("neuprint_fetch_custom", where = ns))))
quit(status = if (ok) 0 else 1)
