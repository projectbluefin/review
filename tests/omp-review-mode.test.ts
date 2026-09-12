/**
 * Contract test for the omp review mode.
 *
 * Runs the real modules against a fake omp host, a fake GitHub, and a real
 * on-disk state tree in a temp directory. No terminal, no network, no omp.
 *
 *   node --test tests/omp-review-mode.test.ts
 */

import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { GLYPH, PLAIN_PAINTER, formatDuration, statusIcon } from "../image/extension/bluefin-review/glyphs.ts";
import { renderSpanTree, traceToText, visibleSpanIds } from "../image/extension/bluefin-review/trace.ts";
import { truncateToWidth, visibleWidth } from "../image/extension/bluefin-review/width.ts";
import { buildPipelineSpans, readStateSnapshot, landingStateStatus, runStateStatus } from "../image/extension/bluefin-review/state.ts";
import { appendFileSync } from "node:fs";
import { fetchDiff, fetchItemsByKey, fetchQueue, parseScope, searchExpression } from "../image/extension/bluefin-review/github.ts";
import { EMPTY_HIVE, buildRankMap, fetchHive, resolveHub } from "../image/extension/bluefin-review/hive.ts";
import { categorize, prioritize } from "../image/extension/bluefin-review/priority.ts";
import { BATCH_LIMIT, ReviewMode } from "../image/extension/bluefin-review/mode.ts";
import { ReviewDashboard } from "../image/extension/bluefin-review/dashboard.ts";
import { STALE_AFTER_MS, queueAge, renderHitlist, renderRail, statusSegment } from "../image/extension/bluefin-review/rail.ts";
import { SessionTrace } from "../image/extension/bluefin-review/session.ts";
import { BluefinAnsiSplash } from "../image/extension/bluefin-review/splash.ts";
import { STATE_ENTRY, actionPrompt, createReviewExtension } from "../image/extension/bluefin-review/extension.ts";

const NOW = 1_800_000_000_000;

// No hub, no home: these tests must not read the developer's own Hive
// registration and must never open a socket.
const ISOLATED_ENV = { GH_TOKEN: "t", HOME: "/nonexistent", XDG_CONFIG_HOME: "/nonexistent" };

// ---------------------------------------------------------------- fixtures

function stateTree(): string {
	const root = mkdtempSync(join(tmpdir(), "bluefin-state-"));
	mkdirSync(join(root, "run-state"), { recursive: true });
	mkdirSync(join(root, "review-batches"), { recursive: true });
	mkdirSync(join(root, "landings"), { recursive: true });
	mkdirSync(join(root, "reviews"), { recursive: true });

	const seconds = NOW / 1000;
	writeFileSync(
		join(root, "run-state", "run-state.json"),
		JSON.stringify({
			version: 1,
			next_sequence: 2,
			records: [
				{
					identity: {
						repository: "projectbluefin/review",
						pull_request: 42,
						base_sha: "a".repeat(40),
						head_sha: "b".repeat(40),
						backend: "omp",
						model: "gemini-3.8-flash",
						effort: "max",
						check_scope_version: "1",
					},
					state: "review_findings",
					terminal_outcome: null,
					reason: "2 high severity findings",
					retry_at: "",
					created_at: seconds - 300,
					updated_at: seconds - 120,
					sequence: 1,
				},
			],
		}),
	);

	const events = [
		{ key: "projectbluefin/review#42", state: "running", note: "review dispatched", ts: seconds - 290 },
		{ key: "projectbluefin/review#42", state: "findings", note: "2 high", ts: seconds - 130, receipt: "r.json" },
		{ key: "projectbluefin/other#7", state: "complete", note: "clean", ts: seconds - 100 },
	];
	writeFileSync(join(root, "review-batches", "batch-1.jsonl"), events.map((e) => JSON.stringify(e)).join("\n") + "\n");

	const landing = [
		{ expect: ["projectbluefin/review#42"], ts: seconds - 120, issues: false, fix: false },
		{ pr: "projectbluefin/review#42", state: "diagnosing", note: "reading failures", ts: seconds - 110 },
		{ pr: "projectbluefin/review#42", state: "fixing", note: "patching launcher", ts: seconds - 90 },
		{ pr: "projectbluefin/review#42", state: "waiting-ci", note: "pushed", ts: seconds - 40 },
	];
	writeFileSync(join(root, "landings", "task-1.jsonl"), landing.map((e) => JSON.stringify(e)).join("\n") + "\n{ torn write");

	writeFileSync(
		join(root, "reviews", "projectbluefin__review__42-abc.json"),
		JSON.stringify({
			version: 1,
			identity: {
				repository: "projectbluefin/review",
				pull_request: 42,
				base_sha: "a".repeat(40),
				head_sha: "b".repeat(40),
				backend: "omp",
				model: "gemini-3.8-flash",
				effort: "max",
				check_scope_version: "1",
			},
			analysis: {
				version: 1,
				state: "findings",
				counts: { critical: 0, high: 2, medium: 1, low: 0 },
				findings: [
					{ severity: "high", file: "image/entrypoint.sh", line: 88, title: "unquoted expansion" },
					{ severity: "high", file: "justfile", line: 412, title: "kubectl fallthrough" },
					{ severity: "medium", file: "tests/just-onboarding.sh", line: 12, title: "missing fake" },
				],
				verification: [
					{ name: "doctrine", state: "verified", evidence: "AGENTS.md read" },
					{ name: "tests", state: "skipped", evidence: "not run" },
				],
			},
			transcript: [],
			provenance: {},
			created_at: "2027-01-15T12:00:00Z",
		}),
	);
	return root;
}

function queueItem(overrides = {}) {
	return {
		id: 42,
		type: "pr",
		repo: "projectbluefin/review",
		title: "fix(launcher): resolve HIVE_HUB before mutating",
		author: "jorge",
		url: "https://github.com/projectbluefin/review/pull/42",
		updatedAt: NOW - 1000,
		draft: false,
		ciStatus: "failure",
		mergeState: "clean",
		reviewState: "review_required",
		labels: ["launcher"],
		additions: 42,
		deletions: 7,
		...overrides,
	};
}

/**
 * Fake GitHub: one GraphQL search page, aliased by-key lookups, and one REST
 * files response.
 *
 * `known` maps `owner/repo#number` to the node returned when the queue asks for
 * that item by name — the path Hive-queued work takes when it falls outside the
 * search window.
 */
function fakeFetch(calls, known = {}) {
	return async (url, init) => {
		calls.push(String(url));
		if (String(url).includes("/graphql")) {
			const body = JSON.parse(String(init?.body ?? "{}"));
			if (body.variables?.search === undefined) {
				const data = {};
				const alias = /(\w+): repository\(owner: "([^"]+)", name: "([^"]+)"\)\s*\{\s*issueOrPullRequest\(number: (\d+)\)/g;
				for (const [, name, owner, repo, number] of body.query.matchAll(alias)) {
					data[name] = { issueOrPullRequest: known[`${owner}/${repo}#${number}`] ?? null };
				}
				return { ok: true, status: 200, statusText: "OK", json: async () => ({ data }) };
			}
			assert.match(body.variables.search, /org:projectbluefin/);
			return {
				ok: true,
				status: 200,
				statusText: "OK",
				json: async () => ({
					data: {
						search: {
							pageInfo: { hasNextPage: false, endCursor: null },
							nodes: [
								{
									number: 42,
									title: "fix(launcher): resolve HIVE_HUB before mutating",
									url: "https://github.com/projectbluefin/review/pull/42",
									updatedAt: new Date(NOW - 1000).toISOString(),
									isDraft: false,
									mergeable: "MERGEABLE",
									reviewDecision: "REVIEW_REQUIRED",
									additions: 42,
									deletions: 7,
									changedFiles: 3,
									author: { login: "jorge" },
									repository: { nameWithOwner: "projectbluefin/review" },
									labels: { nodes: [{ name: "launcher" }] },
									commits: { nodes: [{ commit: { statusCheckRollup: { state: "FAILURE" } } }] },
								},
								{
									number: 7,
									title: "feat(ui): dagger rail",
									url: "https://github.com/projectbluefin/other/pull/7",
									updatedAt: new Date(NOW - 5000).toISOString(),
									mergeable: "MERGEABLE",
									reviewDecision: "APPROVED",
									author: { login: "ada" },
									repository: { nameWithOwner: "projectbluefin/other" },
									labels: { nodes: [] },
									commits: { nodes: [{ commit: { statusCheckRollup: { state: "SUCCESS" } } }] },
								},
							],
						},
					},
				}),
			};
		}
		return {
			ok: true,
			status: 200,
			statusText: "OK",
			json: async () => [
				{ filename: "image/entrypoint.sh", status: "modified", additions: 3, deletions: 1, patch: "@@ -1 +1 @@\n-old\n+new" },
				{ filename: "big.lock", status: "modified", additions: 900, deletions: 900, patch: "x".repeat(50_000) },
			],
		};
	};
}

/** Fake omp extension host recording every registration. */
function fakeHost() {
	const zodLeaf = () => ({ optional: () => zodLeaf(), describe: () => zodLeaf() });
	return {
		labels: [],
		events: new Map(),
		shortcuts: new Map(),
		flags: new Map(),
		flagValues: new Map(),
		tools: new Map(),
		messages: [],
		entries: [],
		zod: { object: () => ({}), string: zodLeaf, number: zodLeaf },
		setLabel(label) {
			this.labels.push(label);
		},
		on(event, handler) {
			this.events.set(event, handler);
		},
		registerShortcut(chord, options) {
			this.shortcuts.set(chord, options);
		},
		registerFlag(name, options) {
			this.flags.set(name, options);
		},
		getFlag(name) {
			return this.flagValues.get(name);
		},
		registerTool(definition) {
			this.tools.set(definition.name, definition);
		},
		sendUserMessage(content) {
			this.messages.push(content);
		},
		appendEntry(customType, data) {
			this.entries.push({ customType, data });
		},
	};
}

/**
 * Fake omp session context.
 *
 * `ui.custom` models a real overlay: it builds the component and resolves only
 * when that component calls `done`. A fake that resolved immediately hid the
 * defect that shipped — a splash which only a keypress could dismiss, awaited
 * inside `session_start` until omp killed the handler.
 */
function fakeCtx() {
	const notifications = [];
	const statuses = new Map();
	const widgets = new Map();
	const overlays = [];
	return {
		hasUI: true,
		notifications,
		statuses,
		widgets,
		overlays,
		pasted: [],
		ui: {
			notify: (message, level) => notifications.push({ message, level }),
			setStatus: (key, value) => statuses.set(key, value),
			setWidget: (key, content) => widgets.set(key, content),
			setTitle: () => {},
			pasteToEditor(text) {
				this.parent.pasted.push(text);
			},
			custom(factory) {
				const { promise, resolve } = Promise.withResolvers();
				overlays.push(factory({ requestRender: () => {} }, this.theme, {}, resolve));
				return promise;
			},
			theme: { fg: (_c, t) => t, bold: (t) => t, inverse: (t) => t },
		},
		sessionManager: { getBranch: () => [] },
	};
}

// ---------------------------------------------------------------- vocabulary

test("duration formatting follows Dagger's units", () => {
	assert.equal(formatDuration(340), "0.3s");
	assert.equal(formatDuration(1234), "1.2s");
	assert.equal(formatDuration(59_949), "59.9s");
	assert.equal(formatDuration(154_000), "2m34s");
	assert.equal(formatDuration(4_325_000), "1h12m5s");
	assert.equal(formatDuration(187_230_000), "2d4h0m30s");
	assert.equal(formatDuration(-1), "");
});

test("status icons are the Dagger glyphs and the spinner cycles", () => {
	assert.equal(statusIcon("success"), "✔");
	assert.equal(statusIcon("failure"), "✘");
	assert.equal(statusIcon("cached"), "$");
	assert.equal(statusIcon("pending"), "○");
	assert.equal(statusIcon("skipped"), "∅");
	assert.equal(statusIcon("running", 0), GLYPH.spinner[0]);
	assert.equal(statusIcon("running", 8), GLYPH.spinner[0]);
	assert.notEqual(statusIcon("running", 1), statusIcon("running", 0));
});

test("width measurement counts cells, not bytes", () => {
	assert.equal(visibleWidth("\u001b[31mred\u001b[0m"), 3);
	assert.equal(visibleWidth("中文"), 4);
	assert.equal(visibleWidth("e\u0301"), 1);
	assert.equal(visibleWidth(truncateToWidth("abcdefghij", 5)), 5);
	// Truncating inside a styled run must not leak color into the next cell.
	assert.ok(truncateToWidth("\u001b[31mabcdefghij\u001b[0m", 5).includes("\u001b[0m"));
});

// ---------------------------------------------------------------- tree

test("tree rails follow the last-child rule", () => {
	const rows = renderSpanTree(
		[
			{
				id: "root",
				label: "root",
				status: "running",
				children: [
					{ id: "a", label: "first", status: "success", children: [{ id: "a1", label: "nested", status: "success" }] },
					{ id: "b", label: "last", status: "pending" },
				],
			},
		],
		{ painter: PLAIN_PAINTER, width: 80, now: NOW, expansion: new Map([["a", true]]) },
	);
	const text = rows.map((r) => r.text);
	assert.ok(text[1].startsWith("├╴"), text[1]);
	// "first" is not the last child, so its own child keeps the parent rail.
	assert.ok(text[2].startsWith("│ ╰╴"), text[2]);
	assert.ok(text[3].startsWith("╰╴"), text[3]);
});

test("collapsed spans hide their children and logs", () => {
	const roots = [
		{
			id: "root",
			label: "root",
			status: "success",
			logs: ["hidden log"],
			children: [{ id: "child", label: "child", status: "success" }],
		},
	];
	const collapsed = renderSpanTree(roots, { painter: PLAIN_PAINTER, width: 80, now: NOW });
	assert.equal(collapsed.length, 1, "a settled span defaults to one line");
	assert.deepEqual(visibleSpanIds(roots), ["root"]);

	const expanded = renderSpanTree(roots, { painter: PLAIN_PAINTER, width: 80, now: NOW, expansion: new Map([["root", true]]) });
	assert.equal(expanded.length, 3);
	assert.ok(expanded[1].text.startsWith("┃ "), expanded[1].text);
});

test("running and failing spans open themselves", () => {
	for (const status of ["running", "failure", "findings"]) {
		const rows = renderSpanTree([{ id: "r", label: "r", status, children: [{ id: "c", label: "c", status: "pending" }] }], {
			painter: PLAIN_PAINTER,
			width: 80,
			now: NOW,
		});
		assert.equal(rows.length, 2, `${status} should expand by default`);
	}
});

test("log tails are capped with a hidden-line header", () => {
	const rows = renderSpanTree([{ id: "r", label: "r", status: "running", logs: ["a", "b", "c", "d", "e"] }], {
		painter: PLAIN_PAINTER,
		width: 80,
		now: NOW,
		maxLogLines: 2,
	});
	assert.ok(rows[1].text.includes("…3 lines hidden…"), rows[1].text);
	assert.ok(rows[2].text.endsWith("d"));
	assert.equal(rows.length, 4);
});

test("rendered rows never exceed the viewport width", () => {
	const rows = renderSpanTree(
		[{ id: "r", label: "x".repeat(400), status: "running", detail: "y".repeat(400), children: [{ id: "c", label: "z".repeat(400), status: "running" }] }],
		{ painter: PLAIN_PAINTER, width: 40, now: NOW },
	);
	for (const row of rows) assert.ok(visibleWidth(row.text) <= 40, `${visibleWidth(row.text)} > 40`);
});

// ---------------------------------------------------------------- state

test("durable state becomes a pipeline trace", (t) => {
	const root = stateTree();
	t.after(() => rmSync(root, { recursive: true, force: true }));

	const snapshot = readStateSnapshot(root);
	assert.equal(snapshot.runs.length, 1);
	assert.equal(snapshot.reviewEvents.length, 3);
	assert.equal(snapshot.landingEvents.filter((e) => e.pullRequestKey).length, 3, "the torn line is skipped");
	assert.equal(snapshot.receipts.get("projectbluefin/review#42")?.findings.length, 3);

	const spans = buildPipelineSpans("projectbluefin/review#42", "fix launcher", snapshot, NOW);
	const text = traceToText(spans, NOW, 120);

	assert.equal(spans[0].status, "running", "landing is still in flight");
	assert.match(text, /run › review_findings/);
	assert.match(text, /review/);
	assert.match(text, /landing/);
	assert.match(text, /waiting-ci/);
	// Only the selected item's events are projected.
	assert.doesNotMatch(text, /other#7/);

	const expanded = traceToText(
		buildPipelineSpans("projectbluefin/review#42", "fix launcher", snapshot, NOW),
		NOW,
		120,
	);
	assert.match(expanded, /image\/entrypoint\.sh:88/, "findings are reachable in the trace");
	assert.match(expanded, /2 high, 1 medium/);
});

test("an unknown pull request reports no state instead of inventing it", (t) => {
	const root = stateTree();
	t.after(() => rmSync(root, { recursive: true, force: true }));
	const spans = buildPipelineSpans("projectbluefin/review#999", "unknown", readStateSnapshot(root), NOW);
	assert.match(traceToText(spans, NOW), /no recorded pipeline state/);
});

test("a missing state root is empty, not an error", () => {
	const snapshot = readStateSnapshot(join(tmpdir(), "bluefin-state-does-not-exist"));
	assert.deepEqual(snapshot.runs, []);
	assert.equal(snapshot.error, undefined);
});

test("appliance states map onto span statuses", () => {
	assert.equal(runStateStatus("reviewing"), "running");
	assert.equal(runStateStatus("review_clean"), "success");
	assert.equal(runStateStatus("review_findings"), "findings");
	assert.equal(runStateStatus("head_changed"), "failure");
	assert.equal(landingStateStatus("waiting-ci"), "running");
	assert.equal(landingStateStatus("merged"), "success");
	assert.equal(landingStateStatus("blocked"), "findings");
	assert.equal(landingStateStatus("failed"), "failure");
});

// ---------------------------------------------------------------- github

test("queue fetch maps CI rollup and reports auth failure", async () => {
	const calls = [];
	const ok = await fetchQueue("prs", { token: "t", fetchImpl: fakeFetch(calls) });
	assert.equal(ok.error, undefined);
	assert.equal(ok.items.length, 2);
	assert.equal(ok.items[0].ciStatus, "failure");
	assert.equal(ok.items[1].ciStatus, "success");
	assert.equal(ok.items[0].repo, "projectbluefin/review");

	const missing = await fetchQueue("prs", { fetchImpl: fakeFetch([]) });
	assert.match(missing.error ?? "", /no GitHub credential/);

	const denied = await fetchQueue("prs", {
		token: "t",
		fetchImpl: async () => ({ ok: false, status: 401, statusText: "Unauthorized", json: async () => ({}) }),
	});
	assert.match(denied.error ?? "", /401/, "a failed queue must say why, not render empty");
});
test("queue cancellation is classified separately from failures", async () => {
	const controller = new AbortController();
	controller.abort();

	const cancelled = await fetchQueue("prs", {
		token: "t",
		signal: controller.signal,
		fetchImpl: async () => {
			throw new Error("request was cancelled");
		},
	});
	assert.equal(cancelled.cancelled, true);
	assert.equal(cancelled.error, undefined);

	const failed = await fetchQueue("prs", {
		token: "t",
		fetchImpl: async () => {
			throw new Error("network down");
		},
	});
	assert.equal(failed.cancelled, undefined);
	assert.equal(failed.error, "network down");
	const named = await fetchItemsByKey(["owner/repo#1"], "prs", {
		token: "t",
		signal: controller.signal,
		fetchImpl: async () => {
			throw new Error("named request was cancelled");
		},
	});
	assert.equal(named.cancelled, true);
	assert.equal(named.error, undefined);
});

test("scope refresh keeps the latest response when requests finish out of order", async () => {
	const requests = [];
	const fetchImpl = (_url, init) => {
		const pending = Promise.withResolvers();
		requests.push({ init, pending });
		return pending.promise;
	};
	const response = (repo, number) => ({
		ok: true,
		status: 200,
		statusText: "OK",
		json: async () => ({
			data: {
				search: {
					pageInfo: { hasNextPage: false },
					nodes: [
						{
							number,
							title: `item ${number}`,
							url: "",
							updatedAt: new Date(NOW).toISOString(),
							author: { login: "a" },
							repository: { nameWithOwner: repo },
							labels: { nodes: [] },
							isDraft: false,
							mergeable: "MERGEABLE",
							reviewDecision: "APPROVED",
							commits: { nodes: [] },
						},
					],
				},
			},
		}),
	});

	const mode = new ReviewMode({
		org: "projectbluefin",
		stateRoot: join(tmpdir(), "nope"),
		fetchImpl,
		env: ISOLATED_ENV,
	});
	mode.setToken("t");
	const staleRefresh = mode.refreshQueue();
	assert.equal(requests.length, 1);

	mode.setScope({ kind: "repo", value: "owner/repo" });
	assert.equal(requests[0].init.signal.aborted, true);
	const currentRefresh = mode.refreshQueue();
	assert.equal(requests.length, 2);

	requests[1].pending.resolve(response("owner/repo", 2));
	await currentRefresh;
	requests[0].pending.resolve(response("projectbluefin/review", 1));
	const staleResult = await staleRefresh;

	assert.equal(staleResult.cancelled, true);
	assert.equal(mode.scopeLabel(), "owner/repo");
	assert.deepEqual(mode.items.map((item) => `${item.repo}#${item.id}`), ["owner/repo#2"]);
	assert.equal(mode.queueError, undefined);
});
test("scope refresh discards stale Hive backfill responses", async () => {
	const requests = [];
	const backfillStarted = Promise.withResolvers();
	const fetchImpl = async (_url, init) => {
		const body = JSON.parse(String(init?.body ?? "{}"));
		const pending = Promise.withResolvers();
		requests.push({ body, init, pending });
		if (body.variables?.search === undefined) backfillStarted.resolve();
		return pending.promise;
	};
	const node = (repo, number) => ({
		number,
		title: `item ${number}`,
		url: "",
		updatedAt: new Date(NOW).toISOString(),
		author: { login: "a" },
		repository: { nameWithOwner: repo },
		labels: { nodes: [] },
		isDraft: false,
		mergeable: "MERGEABLE",
		reviewDecision: "APPROVED",
		commits: { nodes: [] },
	});
	const searchResponse = (item) => ({
		ok: true,
		status: 200,
		statusText: "OK",
		json: async () => ({
			data: { search: { pageInfo: { hasNextPage: false }, nodes: item ? [item] : [] } },
		}),
	});
	const namedResponse = (item) => ({
		ok: true,
		status: 200,
		statusText: "OK",
		json: async () => ({ data: { w0: { issueOrPullRequest: { ...item, closed: false } } } }),
	});

	const mode = new ReviewMode({
		org: "projectbluefin",
		stateRoot: join(tmpdir(), "nope"),
		fetchImpl,
		env: ISOLATED_ENV,
	});
	mode.setToken("t");
	mode.setScope({ kind: "repo", value: "owner/old" });
	const oldKey = "owner/old#9";
	mode.hive = {
		...EMPTY_HIVE,
		configured: true,
		online: true,
		hub: "https://hive.example",
		items: [{ key: oldKey, repo: "owner/old", number: 9, title: "old", url: "", labels: [] }],
		ranks: new Map([[oldKey, 0]]),
	};

	const staleRefresh = mode.refreshQueue();
	assert.equal(requests.length, 1);
	requests[0].pending.resolve(searchResponse(undefined));
	await backfillStarted.promise;

	mode.setScope({ kind: "repo", value: "owner/new" });
	assert.equal(requests[0].init.signal.aborted, true);
	const currentRefresh = mode.refreshQueue();
	assert.equal(requests.length, 3);
	requests[2].pending.resolve(searchResponse(node("owner/new", 2)));
	await currentRefresh;

	requests[1].pending.resolve(namedResponse(node("owner/old", 9)));
	const staleResult = await staleRefresh;

	assert.equal(staleResult.cancelled, true);
	assert.deepEqual(mode.items.map((item) => `${item.repo}#${item.id}`), ["owner/new#2"]);
	assert.deepEqual(mode.hiveCoverage(), { present: 0, total: 0 });
	assert.equal(mode.queueError, undefined);
});


test("diff fetch is bounded but honest about it", async () => {
	const diff = await fetchDiff("projectbluefin/review", 42, { token: "t", fetchImpl: fakeFetch([]), maxPatchChars: 100 });
	assert.equal(diff.totalFiles, 2);
	assert.equal(diff.additions, 903);
	assert.ok(diff.files[0].patch, "the first patch is included");
	assert.ok(diff.files[1].patch === undefined || diff.files[1].patch.includes("truncated"));
	assert.equal(diff.truncated, true);
});

// ---------------------------------------------------------------- mode + ui

test("a queue cut off by the fetch ceiling says so", async () => {
	// Two pages available, ceiling of one item: the counter must not read as total.
	const paged = async (_url, _init) => ({
		ok: true,
		status: 200,
		statusText: "OK",
		json: async () => ({
			data: {
				search: {
					pageInfo: { hasNextPage: true, endCursor: "next" },
					nodes: [
						{
							number: 1,
							title: "one",
							url: "",
							updatedAt: new Date(NOW).toISOString(),
							author: { login: "a" },
							repository: { nameWithOwner: "projectbluefin/review" },
							labels: { nodes: [] },
							commits: { nodes: [] },
						},
					],
				},
			},
		}),
	});

	const result = await fetchQueue("prs", { token: "t", limit: 1, fetchImpl: paged });
	assert.equal(result.truncated, true);

	const mode = new ReviewMode({ org: "projectbluefin", stateRoot: join(tmpdir(), "nope"), fetchImpl: paged });
	mode.setToken("t");
	await mode.refreshQueue();
	assert.match(mode.position(), /\+$/);

	const complete = await fetchQueue("prs", { token: "t", fetchImpl: fakeFetch([]) });
	assert.equal(complete.truncated, false);
});

test("mode ranks, filters, moves, and keeps the selection across a refetch", async () => {
	const mode = new ReviewMode({ org: "projectbluefin", stateRoot: join(tmpdir(), "nope"), fetchImpl: fakeFetch([]), env: ISOLATED_ENV });
	mode.setToken("t");
	await mode.refreshQueue();
	assert.equal(mode.visibleItems().length, 2);

	// Without a hub the local ranking leads: #7 is green and landable, #42 is
	// failing. GitHub's update order put #42 first.
	assert.equal(mode.selected().id, 7);
	assert.equal(mode.priorityFor(mode.selected()).category, "ready-for-human-merge");
	assert.equal(mode.orderSource(), "local");

	mode.move(1);
	assert.equal(mode.selected().id, 42);
	assert.equal(mode.priorityFor(mode.selected()).category, "fix-ci");
	const before = mode.selectedKey();
	await mode.refreshQueue();
	assert.equal(mode.selectedKey(), before, "a refetch must not move the cursor off the item you were reading");

	mode.setFilter("launcher");
	assert.equal(mode.visibleItems().length, 1);
	assert.equal(mode.selected().id, 42);
	mode.setFilter("fix-ci");
	assert.equal(mode.selected().id, 42, "the category is part of the filter surface");
	mode.setFilter("");

	assert.deepEqual(mode.ciTally(), { success: 1, failure: 1, pending: 0, unknown: 0 });
});

test("rail renders the queue, the pipeline, and the keymap within width", (t) => {
	const root = stateTree();
	t.after(() => rmSync(root, { recursive: true, force: true }));
	const mode = new ReviewMode({ org: "projectbluefin", stateRoot: root });
	mode.items = [queueItem()];
	mode.fetchedAt = NOW - 3000;
	mode.refreshState();

	const rows = renderRail(mode, PLAIN_PAINTER, 100, NOW, 0, [{ chord: "alt+b", label: "dashboard" }]);
	assert.ok(rows[0].includes("projectbluefin/review#42") || rows[0].includes("#42"));
	assert.ok(rows[0].includes("alt+b: dash"));
	for (const row of rows) assert.ok(visibleWidth(row) <= 100);

	const multiRows = renderRail(mode, PLAIN_PAINTER, 100, NOW, 0, [{ chord: "alt+b", label: "dashboard" }], { compact: false });
	assert.ok(multiRows[0].includes("⬢ bluefin"));
	assert.ok(multiRows[1].includes("projectbluefin/review#42"));
	assert.ok(multiRows.some((row) => row.includes("landing")), "the rail shows live pipeline stages");
	assert.ok(multiRows[multiRows.length - 1].includes("alt+b"));
	for (const row of multiRows) assert.ok(visibleWidth(row) <= 100);

	assert.match(statusSegment(mode, PLAIN_PAINTER, NOW), /PR 1\/1 #42/);
	mode.selectedKeys.add("projectbluefin/review#42");
	assert.match(statusSegment(mode, PLAIN_PAINTER, NOW), /PR \[1 sel\] 1\/1 #42/);
	mode.selectedKeys.clear();
});
test("hitlist renders window of items around cursor above the editor", (t) => {
	const mode = new ReviewMode({ org: "projectbluefin", stateRoot: join(tmpdir(), "nope") });
	mode.items = [
		queueItem({ id: 1, repo: "projectbluefin/a", title: "first issue" }),
		queueItem({ id: 2, repo: "projectbluefin/b", title: "second issue" }),
		queueItem({ id: 3, repo: "projectbluefin/c", title: "third issue" }),
	];
	mode.cursor = 1;
	const rows = renderHitlist(mode, PLAIN_PAINTER, 80, 5);
	assert.ok(rows[0].includes("HITLIST"));
	assert.ok(rows.some((row) => row.includes("#2") && row.includes(GLYPH.caretClosed)));
	for (const row of rows) assert.ok(visibleWidth(row) <= 80);
});

test("rail explains an empty queue instead of pretending to load forever", () => {
	const mode = new ReviewMode({ org: "projectbluefin", stateRoot: join(tmpdir(), "nope") });
	mode.queueError = "GitHub GraphQL 401 Unauthorized";
	const rows = renderRail(mode, PLAIN_PAINTER, 80, NOW, 0, []);
	assert.match(rows[0], /401/);
});

test("rail and dashboard clarify when queue is empty because of hive-only filter", () => {
	const mode = new ReviewMode({ org: "projectbluefin", stateRoot: join(tmpdir(), "nope") });
	mode.hive = {
		...EMPTY_HIVE,
		configured: true,
		online: true,
		hub: "https://hive.example",
	};
	mode.items = [queueItem({ id: 10, title: "Unranked item" })];
	mode.reprioritize();

	// In default hive-only view, visibleItems is empty because item 10 is unranked
	assert.equal(mode.hiveOnly, true);
	assert.equal(mode.visibleItems().length, 0);

	const railRows = renderRail(mode, PLAIN_PAINTER, 100, NOW, 0, []);
	assert.ok(railRows.some((r) => r.includes("no Hive-ranked prs")));
	assert.ok(railRows.some((r) => r.includes("H shows all")));
	const dashboard = new ReviewDashboard({ requestRender() {} }, PLAIN_PAINTER, mode, () => {}, () => {}, 24);
	const frame = dashboard.render(160);
	dashboard.dispose();
	assert.ok(frame.some((r) => r.includes("no Hive-ranked prs")));
	assert.ok(frame.some((r) => r.includes("press H to show all")));
});
test("queue age is silent while fresh and loud once stale", (t) => {
	assert.equal(queueAge(NOW - 1000, NOW), undefined, "a fresh queue must not repaint a counter every tick");
	assert.match(queueAge(NOW - STALE_AFTER_MS - 1, NOW), /^stale /);
	assert.equal(queueAge(0, NOW), "never fetched");

	const mode = new ReviewMode({ org: "projectbluefin", stateRoot: join(tmpdir(), "nope") });
	mode.items = [queueItem()];
	mode.fetchedAt = NOW - 1000;
	assert.ok(!renderRail(mode, PLAIN_PAINTER, 100, NOW, 0, [])[0].includes("stale"));
	mode.fetchedAt = NOW - STALE_AFTER_MS - 1;
	assert.match(renderRail(mode, PLAIN_PAINTER, 100, NOW, 0, [])[0], /stale/);
});

test("polling durable state reports change, not just activity", (t) => {
	const root = stateTree();
	t.after(() => rmSync(root, { recursive: true, force: true }));
	const mode = new ReviewMode({ org: "projectbluefin", stateRoot: root });

	assert.equal(mode.refreshState(), true, "the first read is a change");
	assert.equal(mode.refreshState(), false, "an unchanged tree must not cost a repaint");

	appendFileSync(
		join(root, "review-batches", "batch-1.jsonl"),
		JSON.stringify({ key: "projectbluefin/review#42", state: "complete", note: "fixed", ts: NOW / 1000 }) + "\n",
	);
	assert.equal(mode.refreshState(), true);
});

test("dashboard navigates, folds, filters, and returns actions", (t) => {
	const root = stateTree();
	t.after(() => rmSync(root, { recursive: true, force: true }));
	const mode = new ReviewMode({ org: "projectbluefin", stateRoot: root });
	mode.items = [queueItem(), queueItem({ id: 7, repo: "projectbluefin/other", title: "feat(ui): dagger rail", ciStatus: "success" })];
	mode.refreshState();

	let action;
	let refreshes = 0;
	const dashboard = new ReviewDashboard(
		{ requestRender() {} },
		PLAIN_PAINTER,
		mode,
		(result) => {
			action = result;
		},
		() => {
			refreshes += 1;
		},
		20,
	);
	t.after(() => dashboard.dispose());

	const frame = () => dashboard.render(120);
	assert.ok(frame()[0].includes("bluefin review"));
	for (const row of frame()) assert.ok(visibleWidth(row) <= 120, row);

	// Ranked order: the green #7 leads, the failing #42 follows.
	assert.equal(mode.selected().id, 7);
	dashboard.handleInput("j");
	assert.equal(mode.selected().id, 42);
	dashboard.handleInput("k");
	assert.equal(mode.selected().id, 7);

	// Trace pane: fold the root and the tree collapses to one row.
	dashboard.handleInput("\t");
	dashboard.handleInput("h");
	const folded = frame().filter((row) => row.includes("review#42")).length;
	dashboard.handleInput("l");
	assert.ok(frame().length >= folded);

	// Filtering is modal and only commits on Enter.
	dashboard.handleInput("\t");
	dashboard.handleInput("/");
	for (const ch of "dagger") dashboard.handleInput(ch);
	assert.equal(mode.filter, "", "filter is a draft until committed");
	dashboard.handleInput("\r");
	assert.equal(mode.visibleItems().length, 1);
	assert.equal(mode.selected().id, 7);
	assert.equal(mode.filter, "dagger");

	dashboard.handleInput("u");
	assert.equal(refreshes, 1);

	dashboard.handleInput("r");
	assert.equal(action.kind, "review");
	assert.equal(action.item.id, 7);

	dashboard.handleInput("\r");
	assert.equal(action.kind, "review");
	assert.equal(action.item.id, 7);
	dashboard.handleInput("*");
	assert.equal(action.kind, "leaderboard", "* opens the hive leaderboard");

	dashboard.handleInput("o");
	assert.equal(action.kind, "scope", "o asks for another repository");
	dashboard.handleInput("q");
	assert.equal(action.kind, "close");

	dashboard.handleInput("?");
	assert.ok(frame().some((row) => row.includes("slay")), "help lists the action keys");
});
test("dashboard supports multi-selection with space, x to clear, and batch action dispatch", (t) => {
	const root = stateTree();
	t.after(() => rmSync(root, { recursive: true, force: true }));
	const mode = new ReviewMode({ org: "projectbluefin", stateRoot: root });
	mode.items = [
		queueItem({ id: 7, repo: "projectbluefin/other", title: "first item", ciStatus: "success" }),
		queueItem({ id: 42, repo: "projectbluefin/review", title: "second item", ciStatus: "failure" }),
	];
	mode.refreshState();

	let action;
	const dashboard = new ReviewDashboard(
		{ requestRender() {} },
		PLAIN_PAINTER,
		mode,
		(result) => {
			action = result;
		},
		() => {},
		20,
	);
	t.after(() => dashboard.dispose());

	// Initially both are unchecked
	assert.ok(dashboard.render(120).some((row) => row.includes("☐")));

	// Select item 7 (and cursor advances to item 42)
	assert.equal(mode.cursor, 0);
	dashboard.handleInput(" ");
	assert.ok(dashboard.render(120).some((row) => row.includes("☒")));
	assert.ok(dashboard.render(120).some((row) => row.includes("1 selected")));
	assert.equal(mode.cursor, 1, "space must advance cursor to next item");

	// Select item 42 directly without pressing j
	dashboard.handleInput(" ");
	assert.ok(dashboard.render(120).some((row) => row.includes("2 selected")));
	// Pressing r dispatches with both items in items array
	dashboard.handleInput("r");
	assert.equal(action.kind, "review");
	assert.equal(action.items.length, 2);
	assert.equal(action.items[0].id, 7);
	assert.equal(action.items[1].id, 42);

	// Clear with x
	dashboard.handleInput("x");
	assert.ok(!dashboard.render(120).some((row) => row.includes("selected)")));
});

test("dashboard stacks panes on a narrow terminal", (t) => {
	const mode = new ReviewMode({ org: "projectbluefin", stateRoot: join(tmpdir(), "nope") });
	mode.items = [queueItem()];
	const dashboard = new ReviewDashboard({ requestRender() {} }, PLAIN_PAINTER, mode, () => {}, () => {}, 20);
	t.after(() => dashboard.dispose());
	const rows = dashboard.render(70);
	assert.ok(rows.some((row) => row.startsWith("▼ QUEUE") || row.startsWith("▶ QUEUE")));
	for (const row of rows) assert.ok(visibleWidth(row) <= 70, row);
});

// ------------------------------------------------------- priority and scope

function prItem(overrides) {
	return queueItem({ ciStatus: "success", mergeState: "clean", reviewState: "review_required", ...overrides });
}

test("without a hub the queue takes the maintainer's order", () => {
	const now = NOW;
	const items = [
		prItem({ id: 1, title: "chore(deps): bump", author: "renovate[bot]", labels: ["deps"], reviewState: "approved" }),
		prItem({ id: 2, title: "draft work", draft: true }),
		prItem({ id: 3, title: "approved change", reviewState: "approved", updatedAt: now - 1000 }),
		prItem({ id: 4, title: "broken change", ciStatus: "failure" }),
		prItem({ id: 5, title: "conflicted change", mergeState: "dirty" }),
		prItem({ id: 6, title: "running checks", ciStatus: "pending" }),
		prItem({ id: 7, title: "reviewed change" }),
	];
	const ranked = prioritize(items, {
		hive: EMPTY_HIVE,
		hasFindings: (key) => key === "projectbluefin/review#7",
		now,
	});

	assert.equal(ranked.source, "local");
	assert.deepEqual(
		ranked.items.map((item) => item.id),
		// Ties inside a category break on recency, then on key, so the order is
		// stable across refetches: #2 and #6 share a timestamp here.
		[3, 1, 7, 5, 4, 2, 6],
		"merge-ready, then review, then conflicts, then CI, then unknowns",
	);
	assert.equal(ranked.priorities.get("projectbluefin/review#3").category, "ready-for-human-merge");
	assert.equal(ranked.priorities.get("projectbluefin/review#7").category, "review");
	assert.match(ranked.priorities.get("projectbluefin/review#7").reason, /recorded review findings/);
	assert.equal(ranked.priorities.get("projectbluefin/review#5").category, "resolve-conflicts");
	assert.equal(ranked.priorities.get("projectbluefin/review#4").category, "fix-ci");
	assert.equal(ranked.priorities.get("projectbluefin/review#2").category, "investigate");

	// A dependency bump is real work, but never the first thing to read: it sinks
	// below the human change in its own category.
	assert.ok(
		ranked.items.findIndex((item) => item.id === 3) < ranked.items.findIndex((item) => item.id === 1),
		"an approved human change outranks an approved bot bump",
	);
	assert.equal(
		categorize(queueItem({ type: "issue" }), { hive: EMPTY_HIVE, hasFindings: () => false, now }).category,
		"triage",
	);
});

test("with a hub the order is Hive's, including through a closing reference", () => {
	const hive = {
		...EMPTY_HIVE,
		configured: true,
		online: true,
		hub: "https://hive.example",
		items: [
			{ key: "projectbluefin/docs#900", repo: "projectbluefin/docs", number: 900, title: "queued", url: "", labels: [], level: "ready" },
			{ key: "projectbluefin/review#5", repo: "projectbluefin/review", number: 5, title: "queued", url: "", labels: [] },
		],
		ranks: buildRankMap(
			[
				{ key: "projectbluefin/docs#900", repo: "projectbluefin/docs", number: 900, title: "", url: "", labels: [], level: "ready" },
				{ key: "projectbluefin/review#5", repo: "projectbluefin/review", number: 5, title: "", url: "", labels: [] },
			],
			[],
		),
	};

	const items = [
		// Green and landable: local ranking would put this first.
		prItem({ id: 3, ciStatus: "success" }),
		// Hive queued the issue this PR closes, so the PR inherits the position.
		prItem({ id: 11, ciStatus: "failure", closingIssues: ["projectbluefin/docs#900"] }),
		prItem({ id: 5, ciStatus: "pending" }),
	];
	const ranked = prioritize(items, { hive, hasFindings: () => false, now: NOW });

	assert.equal(ranked.source, "hive");
	assert.equal(ranked.hiveRanked, 2);
	assert.deepEqual(ranked.items.map((item) => item.id), [11, 5, 3], "Hive's positions are preserved, not recomputed");
	assert.match(ranked.priorities.get("projectbluefin/review#11").reason, /hive ready #1 via projectbluefin\/docs#900/);
	assert.equal(ranked.priorities.get("projectbluefin/review#3").source, "local");
});
test("mode defaults to a hive-only view when hub is online, toggled with H", () => {
	const hive = {
		...EMPTY_HIVE,
		configured: true,
		online: true,
		hub: "https://hive.example",
		items: [{ key: "projectbluefin/review#11", repo: "projectbluefin/review", number: 11, title: "queued", url: "", labels: [] }],
		ranks: buildRankMap([{ key: "projectbluefin/review#11", repo: "projectbluefin/review", number: 11, title: "", url: "", labels: [] }], []),
	};
	const mode = new ReviewMode({ org: "projectbluefin", stateRoot: join(tmpdir(), "nope"), env: ISOLATED_ENV });
	mode.hive = hive;
	mode.items = [
		prItem({ id: 3, ciStatus: "success" }),
		prItem({ id: 11, ciStatus: "success" }),
	];
	mode.reprioritize();

	assert.equal(mode.hiveOnly, true, "default view is hive-only");
	assert.equal(mode.visibleItems().length, 1);
	assert.equal(mode.visibleItems()[0]?.id, 11);

	mode.toggleHiveOnly();
	assert.equal(mode.hiveOnly, false);
	assert.equal(mode.visibleItems().length, 2);

	mode.toggleHiveOnly();
	assert.equal(mode.hiveOnly, true);
	assert.equal(mode.visibleItems().length, 1);
});


test("an unreachable hub degrades to local order and says why", async () => {
	const snapshot = await fetchHive({
		env: { HIVE_HUB: "wss://hive.example/contribute", GH_TOKEN: "t" },
		fetchImpl: async () => ({ ok: false, status: 503, statusText: "Service Unavailable", json: async () => ({}) }),
	});
	assert.equal(snapshot.configured, true);
	assert.equal(snapshot.online, false, "a hub that answers 503 has not answered");
	assert.match(snapshot.error, /503/);
	assert.equal(snapshot.ranks.size, 0);

	// wss:// is the registration form; token-bearing requests use https.
	// Registration names the websocket endpoint; the REST API hangs off the root.
	// Keeping the suffix is what made every call 404 against a real hub.
	assert.equal(resolveHub({ HIVE_HUB: "wss://hive.example/contribute" }), "https://hive.example");
	assert.equal(resolveHub({ HIVE_HUB: "https://hive.example" }), "https://hive.example");
	assert.equal(resolveHub({ HIVE_HUB: "https://user:pw@hive.example" }), "", "no token to a URL carrying credentials");
	assert.equal(resolveHub({ HIVE_HUB: "http://hive.example" }), "", "no token over plaintext");
	assert.equal(resolveHub({ HIVE_HUB: "https://a,https://b" }), "", "an unmade selection is not a hub");
	assert.equal(resolveHub({ HOME: "/nonexistent" }), "");
});

test("a hub's queue and triage become one rank map in Hive's order", async () => {
	const calls = [];
	const snapshot = await fetchHive({
		env: { HIVE_HUB: "https://hive.example", GH_TOKEN: "t" },
		fetchImpl: async (url) => {
			calls.push(String(url));
			const path = String(url).replace("https://hive.example", "");
			const body =
				path === "/api/v1/status"
					? { hub: "online", actionable_items: 122 }
					: path === "/api/v1/contributors"
						? {
								contributors: [
									{
										github_username: "danathar",
										active: true,
										current_task: { key: "projectbluefin/review#42", repo: "projectbluefin/review", number: 42 },
									},
									// Offline workers hold nothing: their last task is history.
									{
										github_username: "ghost",
										active: false,
										current_task: { key: "projectbluefin/docs#1092", repo: "projectbluefin/docs", number: 1092 },
									},
								],
							}
					: path === "/api/contribute/queue"
						? { queue: [{ repo: "projectbluefin/docs", number: 1092, title: "fix pin state", labels: ["3-clanker-queue"] }] }
						: {
								groups: [
									{
										level: "reviewing",
										label: "Reviewing",
										count: 2,
										issues: [
											{ repo: "projectbluefin/review", number: 42, title: "under review" },
											// The same work the ready queue already listed. Hive says
											// what stage it is at and which change answers it; only
											// the queue says where it sits.
											{
												repo: "projectbluefin/docs",
												number: 1092,
												title: "fix pin state",
												pr: { number: 1170, url: "https://github.com/projectbluefin/docs/pull/1170", state: "open" },
											},
										],
									},
								],
							};
			return { ok: true, status: 200, statusText: "OK", json: async () => body };
		},
	});

	assert.equal(snapshot.online, true);
	assert.equal(snapshot.actionableItems, 122);
	assert.deepEqual([...snapshot.ranks.entries()], [
		["projectbluefin/docs#1092", 0],
		["projectbluefin/review#42", 1],
	]);
	assert.deepEqual(snapshot.triage, [{ level: "reviewing", label: "Reviewing", count: 2 }]);

	// One entry per key: the queue's position, the triage view's stage and link.
	assert.equal(snapshot.items.length, 2);
	const queued = snapshot.items.find((item) => item.key === "projectbluefin/docs#1092");
	assert.equal(queued.level, "reviewing", "an item's stage must survive being in both views");
	assert.deepEqual(queued.pr, { number: 1170, url: "https://github.com/projectbluefin/docs/pull/1170", state: "open" });
	assert.deepEqual(queued.labels, ["3-clanker-queue"], "the queue view's labels are kept");

	// Who is already on something, so a batch never duplicates a live worker.
	assert.deepEqual([...snapshot.claims.entries()], [["projectbluefin/review#42", "danathar"]]);
	assert.equal(calls.length, 4);
});

test("a repository scope is parsed strictly and reaches the search", () => {
	assert.deepEqual(parseScope("owner/repo", "projectbluefin"), { kind: "repo", value: "owner/repo" });
	assert.deepEqual(parseScope("bluefin", "projectbluefin"), { kind: "repo", value: "projectbluefin/bluefin" });
	assert.deepEqual(parseScope("https://github.com/owner/repo.git", "projectbluefin"), { kind: "repo", value: "owner/repo" });
	assert.deepEqual(parseScope("org:someorg", "projectbluefin"), { kind: "org", value: "someorg" });
	assert.equal(parseScope("not a repo", "projectbluefin"), undefined);
	assert.equal(parseScope("  ", "projectbluefin"), undefined);

	assert.match(searchExpression("prs", { kind: "repo", value: "owner/repo" }), /^repo:owner\/repo is:pr /);
	assert.match(searchExpression("issues", { kind: "org", value: "projectbluefin" }), /^org:projectbluefin is:issue /);
});

test("scoping the queue to one repository refetches and persists", async () => {
	const searches = [];
	const scoped = async (url, init) => {
		const body = JSON.parse(String(init?.body ?? "{}"));
		searches.push(body.variables.search);
		return {
			ok: true,
			status: 200,
			statusText: "OK",
			json: async () => ({ data: { search: { pageInfo: { hasNextPage: false }, nodes: [] } } }),
		};
	};
	const mode = new ReviewMode({ org: "projectbluefin", stateRoot: join(tmpdir(), "nope"), fetchImpl: scoped, env: ISOLATED_ENV });
	mode.setToken("t");
	mode.setScope(parseScope("owner/repo", "projectbluefin"));
	await mode.refreshQueue();

	assert.equal(mode.scopeLabel(), "owner/repo");
	assert.match(searches[0], /repo:owner\/repo/);
	assert.deepEqual(mode.toPersisted().scope, { kind: "repo", value: "owner/repo" });
});

// ---------------------------------------------------------------- session trace

test("tool executions become spans on the current turn", () => {
	const trace = new SessionTrace();
	trace.startTurn(NOW);
	trace.startTool("c1", "bash", { command: "just review-doctor\nsecond line" }, NOW + 100);
	trace.updateTool("c1", "checking podman");
	assert.equal(trace.active().label, "bash(just review-doctor)");
	trace.endTool("c1", { content: [{ type: "text", text: "ok" }] }, false, NOW + 900);
	trace.endTurn(NOW + 1000);

	const turn = trace.roots()[0];
	assert.equal(turn.status, "success");
	assert.equal(turn.children[0].status, "success");
	assert.deepEqual(turn.children[0].logs, ["ok"]);
	assert.equal(trace.active(), undefined);

	const failing = new SessionTrace();
	failing.startTool("c2", "read", { path: "AGENTS.md" }, NOW);
	failing.endTool("c2", "boom", true, NOW + 5);
	failing.endTurn(NOW + 6);
	assert.equal(failing.roots()[0].status, "failure", "a failed tool fails its turn");
});

// ---------------------------------------------------------------- extension

test("the extension registers keyboard-only surfaces and real tools", async () => {
	const pi = fakeHost();
	const review = createReviewExtension(pi, { org: "projectbluefin", fetchImpl: fakeFetch([]), env: ISOLATED_ENV });

	assert.deepEqual(pi.labels, ["Bluefin Review"]);
	assert.deepEqual([...pi.shortcuts.keys()].sort(), ["alt+b", "alt+i", "alt+j", "alt+k", "alt+o", "alt+u", "alt+x", "alt+y"]);
	assert.deepEqual([...pi.flags.keys()].sort(), ["all", "issues", "pr", "repo", "splash"]);
	assert.deepEqual([...pi.tools.keys()].sort(), [
		"bluefin_hive_lookup",
		"bluefin_review_diff",
		"bluefin_review_queue",
		"bluefin_review_status",
		"bluefin_review_trace",
	]);
	// Reserved chords would be silently dropped by omp.
	for (const chord of pi.shortcuts.keys()) assert.match(chord, /^alt\+[a-z]$/);

	const ctx = fakeCtx();
	ctx.ui.parent = ctx;
	pi.flagValues.set("splash", false);
	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();
	// Ranked, not fetched-order: #7 is green and landable, #42 is failing.
	assert.ok(ctx.statuses.get("bluefin_queue")?.includes("#7"), ctx.statuses.get("bluefin_queue"));
	assert.equal(typeof ctx.widgets.get("bluefin-rail"), "function", "the rail is a component, not capped strings");
	assert.equal(ctx.widgets.get("bluefin-hitlist"), undefined, "hitlist widget is removed to avoid editor crowding");

	const diff = await pi.tools.get("bluefin_review_diff").execute("id", { pull_request: 42 });
	assert.match(diff.content[0].text, /image\/entrypoint\.sh/);
	assert.equal(diff.details.pull_request, 42);
	assert.equal(diff.details.repo, "projectbluefin/review");
	const bad = await pi.tools.get("bluefin_review_diff").execute("id", { pull_request: 0 });
	assert.equal(bad.isError, true);

	const status = await pi.tools.get("bluefin_review_status").execute("id", {});
	assert.match(status.content[0].text, /selected projectbluefin\/other#7/);
	// The model must be told which authority ordered the queue, and that Hive's
	// order is not its to rearrange.
	assert.match(status.content[0].text, /order: local — no hive hub configured/);
	assert.match(status.content[0].text, /priority: ready-for-human-merge/);
	assert.equal(status.details.order_source, "local");
	assert.equal(status.details.hive.configured, false);
	assert.equal(status.details.scope.value, "projectbluefin");

	const queue = await pi.tools.get("bluefin_review_queue").execute("id", {});
	assert.match(queue.content[0].text, /^\[ready-for-human-merge\] projectbluefin\/other#7/m);
	assert.equal(queue.details.order_source, "local");

	pi.events.get("tool_execution_start")({ toolCallId: "x", toolName: "grep", args: { pattern: "HIVE_HUB" } }, ctx);
	const live = await pi.tools.get("bluefin_review_status").execute("id", {});
	assert.equal(live.details.selected.id, 7);
	const lookup = await pi.tools.get("bluefin_hive_lookup").execute("id", {});
	assert.match(lookup.content[0].text, /Hive hub is not configured/);
	// Startup opens the dashboard itself, and it stays open until the maintainer
	// closes it, so alt+b on an open dashboard must not stack a second overlay.
	assert.equal(ctx.overlays.length, 1, "startup opens exactly one dashboard");
	await pi.shortcuts.get("alt+b").handler(ctx);
	assert.equal(ctx.overlays.length, 1, "alt+b on an open dashboard opens nothing new");
});

// The timeout is the assertion: a handler that waits on its own work never
// returns here, and node:test turns that into a failure instead of a hung suite.
test("session_start returns without waiting for the queue or the intro", { timeout: 5000 }, async () => {
	const pi = fakeHost();
	// A GitHub that never answers and an intro nobody dismisses. omp kills an
	// extension handler that has not returned inside its budget, and everything
	// that keeps the queue fresh — the state, queue and hub poll timers — is
	// registered by this handler. Blocking here cost the session all three.
	const stalled = Promise.withResolvers();
	const review = createReviewExtension(pi, {
		org: "projectbluefin",
		fetchImpl: () => stalled.promise,
		env: ISOLATED_ENV,
	});
	const ctx = fakeCtx();
	ctx.ui.parent = ctx;

	await pi.events.get("session_start")({}, ctx);

	// Handed back mid-flight, not by skipping the work: the intro is on screen
	// and the rail is mounted to show the queue arriving behind it.
	assert.equal(ctx.overlays.length, 1, "the intro is still open when the handler returns");
	assert.equal(typeof ctx.widgets.get("bluefin-rail"), "function", "the rail is mounted before the first await");

	// Let the stalled reads answer and dismiss the intro: startup then completes
	// on its own and lands the maintainer in the dashboard.
	stalled.resolve({ ok: false, status: 504, statusText: "Gateway Timeout", json: async () => ({}) });
	ctx.overlays[0].handleInput(" ");
	await review.whenStarted();
	assert.equal(ctx.overlays.length, 2, "the dashboard opens once the intro is done");
	assert.ok(
		ctx.notifications.some((entry) => entry.level === "error" && /504/.test(entry.message)),
		"a queue that failed says so instead of rendering as an empty queue",
	);
});
test("refresh cancellation is silent while replacement activity continues", async () => {
	const pi = fakeHost();
	const firstStarted = Promise.withResolvers();
	const firstCancelled = Promise.withResolvers();
	let calls = 0;
	const fetchImpl = async (_url, init) => {
		calls += 1;
		if (calls === 1) {
			firstStarted.resolve();
			const firstRequest = Promise.withResolvers();
			init.signal.addEventListener(
				"abort",
				() => {
					firstCancelled.resolve();
					firstRequest.reject(new Error("The operation was aborted"));
				},
				{ once: true },
			);
			await firstRequest.promise;
		}
		return {
			ok: true,
			status: 200,
			statusText: "OK",
			json: async () => ({ data: { search: { pageInfo: { hasNextPage: false }, nodes: [] } } }),
		};
	};
	const review = createReviewExtension(pi, { org: "projectbluefin", fetchImpl, env: ISOLATED_ENV });
	const ctx = fakeCtx();
	ctx.ui.parent = ctx;
	pi.flagValues.set("splash", false);

	await pi.events.get("session_start")({}, ctx);
	await firstStarted.promise;
	await pi.shortcuts.get("alt+u").handler(ctx);
	await firstCancelled.promise;
	await review.whenStarted();

	assert.equal(calls, 2);
	assert.equal(
		ctx.notifications.some((entry) => /aborted/.test(entry.message)),
		false,
		"an expected cancellation must not render as a queue error",
	);
});

test("a headless session hands the queue to the tools, not to the handler", async () => {
	const pi = fakeHost();
	createReviewExtension(pi, { org: "projectbluefin", fetchImpl: fakeFetch([]), env: ISOLATED_ENV });
	const ctx = fakeCtx();
	ctx.hasUI = false;
	ctx.ui.parent = ctx;

	await pi.events.get("session_start")({}, ctx);
	// No rail, no intro, no dashboard: there is nothing to render into.
	assert.equal(ctx.overlays.length, 0);
	assert.equal(ctx.widgets.size, 0);

	// The handler returned before the fetch landed, so the tool has to wait for
	// it. Reading the queue too early is how a print-mode run reports that a busy
	// organization has nothing open.
	const queue = await pi.tools.get("bluefin_review_queue").execute("id", {});
	assert.match(queue.content[0].text, /projectbluefin\/other#7/);
	assert.equal(queue.details.items.length, 2);
});

test("the intro dismisses itself instead of holding the session open", (t) => {
	t.mock.timers.enable({ apis: ["setInterval"] });
	let dismissals = 0;
	const timed = new BluefinAnsiSplash({ requestRender: () => {} }, () => dismissals++);
	t.mock.timers.tick(10_000);
	assert.equal(dismissals, 1, "the intro must end on its own, and exactly once");
	timed.dispose();

	// A keypress still ends it early; that is the affordance the intro advertises.
	let pressed = 0;
	const interactive = new BluefinAnsiSplash({ requestRender: () => {} }, () => pressed++);
	interactive.handleInput(" ");
	t.mock.timers.tick(10_000);
	assert.equal(pressed, 1, "a keypress dismisses the intro once, and the timer cannot repeat it");
});

test("a resumed session reopens on the item it left", async () => {
	const pi = fakeHost();
	const review = createReviewExtension(pi, { org: "projectbluefin", fetchImpl: fakeFetch([]), env: ISOLATED_ENV });

	const ctx = fakeCtx();
	ctx.ui.parent = ctx;
	// The queue is empty until the fetch lands, so a selection restored at
	// session_start alone would silently fall back to the first row.
	ctx.sessionManager = {
		getBranch: () => [
			{ type: "custom", customType: STATE_ENTRY, data: { mode: "prs", repo: "projectbluefin/other", id: 7 } },
		],
	};
	pi.flagValues.set("splash", false);
	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();
	assert.ok(ctx.statuses.get("bluefin_queue")?.includes("#7"));
});

test("the status tool names the authority that ordered the queue", async () => {
	// A hub that answers, with a queue that matches nothing in this scope.
	const hiveFetch = (ranked) => async (url, init) => {
		const target = String(url);
		if (target.includes("/graphql")) return fakeFetch([])(url, init);
		const path = target.replace("https://hive.example", "");
		const body =
			path === "/api/v1/status"
				? { hub: "online", actionable_items: 122 }
				: path === "/api/contribute/queue"
					? { queue: ranked }
					: { groups: [] };
		return { ok: true, status: 200, statusText: "OK", json: async () => body };
	};

	const statusFor = async (fetchImpl, env) => {
		const pi = fakeHost();
		const review = createReviewExtension(pi, { org: "projectbluefin", fetchImpl, env });
		const ctx = fakeCtx();
		ctx.ui.parent = ctx;
		pi.flagValues.set("splash", false);
		await pi.events.get("session_start")({}, ctx);
		await review.whenStarted();
		return await pi.tools.get("bluefin_review_status").execute("id", {});
	};

	const hubEnv = { ...ISOLATED_ENV, HIVE_HUB: "https://hive.example" };

	const noHub = await statusFor(fakeFetch([]), ISOLATED_ENV);
	assert.match(noHub.content[0].text, /order: local — no hive hub configured/);
	assert.equal(noHub.details.hive.configured, false);

	const quiet = await statusFor(hiveFetch([]), hubEnv);
	assert.match(quiet.content[0].text, /order: local — hive is online .* has queued nothing in this scope/);
	assert.equal(quiet.details.hive.online, true);
	assert.equal(quiet.details.order_source, "local", "an online hub with nothing to say does not order the queue");

	// The same hub, now ranking one of the fetched pull requests.
	const ranking = await statusFor(
		hiveFetch([{ repo: "projectbluefin/review", number: 42, title: "queued" }]),
		hubEnv,
	);
	assert.match(ranking.content[0].text, /order: hive — 1 of 2 items ranked/);
	assert.match(ranking.content[0].text, /Hive owns priority; do not reorder or reassign it/);
	assert.equal(ranking.details.order_source, "hive");
	assert.match(ranking.content[0].text, /selected projectbluefin\/review#42/);

	const unreachable = await statusFor(async (url, init) => {
		if (String(url).includes("/graphql")) return fakeFetch([])(url, init);
		return { ok: false, status: 502, statusText: "Bad Gateway", json: async () => ({}) };
	}, hubEnv);
	assert.match(unreachable.content[0].text, /order: local — hive configured but unreachable \(.*502/);
});


test("action prompts name the evidence and refuse to merge red checks", () => {
	const item = queueItem();
	assert.match(actionPrompt({ kind: "review", item }), /bluefin_review_diff/);
	assert.match(actionPrompt({ kind: "review", item }), /bluefin_review_trace/);
	const approve = actionPrompt({ kind: "approve", item });
	assert.match(approve, /gh pr checks 42 --repo projectbluefin\/review/);
	assert.match(approve, /Stop and report instead of merging/);
	assert.match(actionPrompt({ kind: "fix", item }), /Do not suppress a finding/);
	const docs = actionPrompt({ kind: "docs", item });
	assert.match(docs, /projectbluefin\/common agentic documentation system/);
	assert.match(docs, /check-skill-frontmatter\.sh/);
	const batchAction = { kind: "review", item, items: [item, queueItem({ id: 7, repo: "projectbluefin/other" })] };
	const batchPrompt = actionPrompt(batchAction);
	assert.match(batchPrompt, /k3-final-review/);
	assert.match(batchPrompt, /Kimi K3 at max effort/);
	assert.match(batchPrompt, /Repository `projectbluefin\/review`/);
	assert.match(batchPrompt, /Repository `projectbluefin\/other`/);
	assert.match(batchPrompt, /cross-repository contract compatibility/);
	assert.equal(actionPrompt({ kind: "close" }), undefined);
});

test("slaying an issue ships a pull request for someone else to merge", () => {
	const issue = queueItem({ id: 936, type: "issue", repo: "projectbluefin/documentation", title: "npm test misses scripts/lib" });
	const prompt = actionPrompt({ kind: "slay", item: issue });
	assert.match(prompt, /open a pull request/);
	assert.match(prompt, /Closes projectbluefin\/documentation#936/);
	assert.match(prompt, /never merge your own/);
	assert.doesNotMatch(prompt, /review the diff/, "an issue has no diff to land");

	// A pull request still gets the landing pass; the key means two things.
	const landing = actionPrompt({ kind: "slay", item: queueItem() });
	assert.match(landing, /Run the full landing pass/);
	assert.match(landing, /Do not merge without green checks/);

	// A batch of issues is still one pull request per issue, not one for the lot.
	const batch = [issue, queueItem({ id: 941, type: "issue", repo: "projectbluefin/documentation" })];
	const batchPrompt = actionPrompt({ kind: "slay", item: issue, items: batch });
	assert.match(batchPrompt, /one pull request per issue/);
	assert.match(batchPrompt, /never merge your own/);
	assert.match(batchPrompt, /report an evidenced finding/);

	// A mixed selection cannot be both, so it keeps the landing pass it had.
	const mixed = actionPrompt({ kind: "slay", item: issue, items: [issue, queueItem()] });
	assert.match(mixed, /Run the full landing pass/);
});

test("hive work the search never returned is still admitted to the queue", async () => {
	// Hive queues an issue that is nowhere near the top of a recency-ordered
	// search. Without admitting it by name, the tool whose purpose is burning
	// Hive's queue down would never show it.
	const queued = [
		{ repo: "projectbluefin/lab", number: 470, title: "vanilla-kde: boot KDE natively" },
		{ repo: "projectbluefin/documentation", number: 936, title: "npm test misses scripts/lib" },
		// Hive keeps ranking work after it is finished. A queue is what is left.
		{ repo: "projectbluefin/lab", number: 466, title: "Gate C soak" },
	];
	const known = {
		"projectbluefin/lab#470": {
			number: 470,
			title: "vanilla-kde: boot KDE natively",
			url: "https://github.com/projectbluefin/lab/issues/470",
			updatedAt: new Date(NOW - 90 * 24 * 3600 * 1000).toISOString(),
			author: { login: "castrojo" },
			repository: { nameWithOwner: "projectbluefin/lab" },
			labels: { nodes: [] },
			closed: false,
		},
		"projectbluefin/lab#466": {
			number: 466,
			title: "Gate C soak",
			url: "https://github.com/projectbluefin/lab/issues/466",
			updatedAt: new Date(NOW - 40 * 24 * 3600 * 1000).toISOString(),
			author: { login: "castrojo" },
			repository: { nameWithOwner: "projectbluefin/lab" },
			labels: { nodes: [] },
			closed: true,
		},
	};
	const hubFetch = async (url, init) => {
		const target = String(url);
		if (target.includes("/graphql")) return fakeFetch([], known)(url, init);
		const path = target.replace("https://hive.example", "");
		const body =
			path === "/api/v1/status"
				? { hub: "online", actionable_items: 108 }
				: path === "/api/contribute/queue"
					? { queue: queued }
					: { groups: [] };
		return { ok: true, status: 200, statusText: "OK", json: async () => body };
	};

	const pi = fakeHost();
	const review = createReviewExtension(pi, {
		org: "projectbluefin",
		fetchImpl: hubFetch,
		env: { ...ISOLATED_ENV, HIVE_HUB: "https://hive.example" },
	});
	const ctx = fakeCtx();
	ctx.ui.parent = ctx;
	pi.flagValues.set("splash", false);
	pi.flagValues.set("issues", true);
	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();

	const queue = await pi.tools.get("bluefin_review_queue").execute("id", {});
	assert.match(queue.content[0].text, /projectbluefin\/lab#470/, "hive work outside the search must still be queued");
	assert.equal(queue.details.order_source, "hive");

	// #936 resolved to nothing — moved or unreadable — and #466 came back closed.
	// Neither is in the queue, and the shortfall is reported rather than hidden
	// behind a list that merely looks complete.
	assert.doesNotMatch(queue.content[0].text, /#936/);
	assert.doesNotMatch(queue.content[0].text, /#466/, "finished work is not a queue");
	const status = await pi.tools.get("bluefin_review_status").execute("id", {});
	assert.equal(status.details.hive.queued.present, 1);
	assert.equal(status.details.hive.queued.total, 3);
});

test("the dashboard drills into Hive's queue by stage and explains each item", (t) => {
	const root = stateTree();
	t.after(() => rmSync(root, { recursive: true, force: true }));
	const mode = new ReviewMode({ org: "projectbluefin", stateRoot: root });

	const ready = { key: "projectbluefin/lab#470", repo: "projectbluefin/lab", number: 470, title: "vanilla-kde", url: "https://github.com/projectbluefin/lab/issues/470", labels: ["gate"], level: "ready" };
	const triaging = { key: "projectbluefin/docs#936", repo: "projectbluefin/docs", number: 936, title: "npm test gap", url: "", labels: [], level: "triaging" };
	mode.hive = {
		...EMPTY_HIVE,
		hub: "https://hive.example",
		configured: true,
		online: true,
		items: [ready, triaging],
		triage: [
			{ level: "triaging", label: "Triaging", count: 1 },
			{ level: "ready", label: "Ready to implement", count: 1 },
		],
		ranks: new Map([[ready.key, 0], [triaging.key, 1]]),
	};
	mode.items = [
		queueItem({ id: 470, type: "issue", repo: "projectbluefin/lab", title: "vanilla-kde" }),
		queueItem({ id: 936, type: "issue", repo: "projectbluefin/docs", title: "npm test gap" }),
		// A pull request that closes the ready issue: work already exists for it.
		queueItem({ id: 12, repo: "projectbluefin/lab", title: "feat: boot KDE under OVMF", ciStatus: "success", closingIssues: [ready.key] }),
	];
	mode.refreshState();
	mode.reprioritize();

	// L steps through Hive's own stages, in the hub's order, then back to all.
	assert.equal(mode.visibleItems().length, 3);
	assert.equal(mode.cycleHiveLevel(), "triaging");
	assert.deepEqual(mode.visibleItems().map((item) => item.id), [936]);
	assert.equal(mode.cycleHiveLevel(), "ready");
	assert.deepEqual(mode.visibleItems().map((item) => item.id).sort(), [12, 470]);
	assert.equal(mode.cycleHiveLevel(), undefined, "the last stage returns to the whole queue");
	assert.equal(mode.visibleItems().length, 3);

	const dashboard = new ReviewDashboard({ requestRender() {} }, PLAIN_PAINTER, mode, () => {}, () => {}, 24);
	t.after(() => dashboard.dispose());
	dashboard.handleInput("L");
	assert.equal(mode.hiveLevel, "triaging", "L is bound to the stage walk");

	// Select the ready issue and read the detail pane: rank, stage, and the open
	// change that already answers it.
	mode.hiveLevel = undefined;
	mode.selectById("projectbluefin/lab", 470);
	const detail = dashboard.render(120).join("\n");
	assert.match(detail, /hive #1/);
	assert.match(detail, /stage ready/);
	assert.match(detail, /#12 feat: boot KDE under OVMF/, "an open change closing the issue is shown");

	// The issue nobody has answered says so instead of leaving the pane blank.
	mode.selectById("projectbluefin/docs", 936);
	const unanswered = dashboard.render(120).join("\n");
	assert.match(unanswered, /stage triaging/);
	assert.match(unanswered, /no open change closes this yet/);
});

test("a filtered slice is selected and dispatched in one wave", (t) => {
	const root = stateTree();
	t.after(() => rmSync(root, { recursive: true, force: true }));
	const mode = new ReviewMode({ org: "projectbluefin", stateRoot: root });
	mode.items = Array.from({ length: 40 }, (_, i) =>
		queueItem({ id: 100 + i, type: "issue", repo: "projectbluefin/lab", title: `queued work ${i}` }),
	);
	mode.reprioritize();

	// One key takes the whole slice the filters left, up to the dispatch ceiling.
	assert.equal(mode.selectAllVisible(), BATCH_LIMIT, "a burn-down selects a slice, not a row");
	assert.equal(mode.chosenItems().length, BATCH_LIMIT);
	// Pressing it again on a fully selected slice clears it: one key, both ways.
	mode.items = mode.items.slice(0, BATCH_LIMIT);
	mode.reprioritize();
	assert.equal(mode.selectAllVisible(), 0);
	assert.equal(mode.chosenItems().length, 0);

	// It respects the filters, so a stage or a search is what gets dispatched.
	mode.filter = "work 1";
	const narrowed = mode.selectAllVisible();
	assert.equal(narrowed, mode.visibleItems().length);
	assert.ok(narrowed > 1 && narrowed < BATCH_LIMIT, `expected a narrowed slice, got ${narrowed}`);

	const dashboard = new ReviewDashboard({ requestRender() {} }, PLAIN_PAINTER, mode, () => {}, () => {}, 24);
	t.after(() => dashboard.dispose());
	mode.clearSelected();
	dashboard.handleInput("A");
	assert.equal(mode.selectedKeys.size, narrowed, "A is bound to the slice selection");

	// The dispatched prompt must fan out. A batch worked top to bottom is a list.
	const batch = mode.chosenItems();
	const prompt = actionPrompt({ kind: "slay", item: batch[0], items: batch });
	assert.match(prompt, /concurrently/);
	assert.match(prompt, /one agent per item/);
	assert.match(prompt, /Do not process the list sequentially/);
	assert.match(prompt, /name every item that failed/);
	assert.doesNotMatch(prompt, /repository sequence/);
});
