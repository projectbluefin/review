/**
 * Contract test for the omp review mode.
 *
 * Runs the real modules against a fake OMP host and fake GitHub/Hive APIs.
 * No terminal, network, or OMP process is required.
 *
 *   node --test tests/omp-review-mode.test.ts
 */

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { GLYPH, PLAIN_PAINTER, formatDuration, statusIcon } from "../image/extension/bluefin-review/glyphs.ts";
import { workbenchPainter } from "../image/extension/bluefin-review/paint.ts";
import { renderSpanTree, traceToText, visibleSpanIds } from "../image/extension/bluefin-review/trace.ts";
import { truncateToWidth, visibleWidth } from "../image/extension/bluefin-review/width.ts";
import { fetchDiff, exactHeadVerified, fetchItemsByKey, fetchQueue, parseScope, searchExpression, toCiStatus } from "../image/extension/bluefin-review/github.ts";
import { EMPTY_HIVE, buildRankMap, fetchHive, hiveFailureStatus, resolveHub } from "../image/extension/bluefin-review/hive.ts";
import { categorize, prioritize } from "../image/extension/bluefin-review/priority.ts";
import { BATCH_LIMIT, ReviewMode, ciGlyph } from "../image/extension/bluefin-review/mode.ts";
import { HOLD_LABELS, isLandingReady, landingReason, landingState } from "../image/extension/bluefin-review/landing.ts";
import { RAW_KEYS, canonicalKey, rawKeyMatcher } from "../image/extension/bluefin-review/keys.ts";
import { ReviewDashboard, parseMouseEvent } from "../image/extension/bluefin-review/dashboard.ts";
import { STALE_AFTER_MS, priorityChip, queueAge, renderRail, statusSegment } from "../image/extension/bluefin-review/rail.ts";
import { SessionTrace } from "../image/extension/bluefin-review/session.ts";
import {
	STATE_ENTRY,
	BATCH_ENTRY,
	COMMENT_ENTRY,
	BLUEFIN_POLICY,
	actionPrompt,
	createReviewExtension,
	createCommentActionPlan,
	renderCommentActionPlan,
	validateCommentActionPlan,
	commentInvocation,
} from "../image/extension/bluefin-review/extension.ts";

const NOW = 1_800_000_000_000;

// No hub, no home: these tests must not read the developer's own Hive
// registration and must never open a socket.
const ISOLATED_ENV = { GH_TOKEN: "t", HOME: "/nonexistent", XDG_CONFIG_HOME: "/nonexistent" };

test("every review extension module is reachable from its package entrypoint", () => {
	const directory = join(process.cwd(), "image/extension/bluefin-review");
	const modules = new Set(readdirSync(directory).filter((name) => name.endsWith(".ts")));
	const reachable = new Set<string>();
	const pending = ["index.ts"];
	const importPattern = /(?:from\s+|import\s*)(["'])(\.\/[^"']+)\1/g;
	while (pending.length > 0) {
		const name = pending.pop();
		if (!name || reachable.has(name)) continue;
		reachable.add(name);
		const source = readFileSync(join(directory, name), "utf8");
		for (const match of source.matchAll(importPattern)) {
			const dependency = match[2]!.slice(2);
			if (modules.has(dependency) && !reachable.has(dependency)) pending.push(dependency);
		}
	}

	assert.deepEqual([...modules].filter((name) => !reachable.has(name)).sort(), []);
});

// ---------------------------------------------------------------- fixtures


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
					const key = `${owner}/${repo}#${number}`;
					const fallback = key === "projectbluefin/review#42"
						? {
							number: 42,
							title: "fix(launcher): resolve HIVE_HUB before mutating",
							url: "https://github.com/projectbluefin/review/pull/42",
							updatedAt: new Date(NOW - 1000).toISOString(),
							isDraft: false,
							mergeable: "MERGEABLE",
							reviewDecision: "REVIEW_REQUIRED",
							headRefOid: "4".repeat(40),
							author: { login: "jorge" },
							repository: { nameWithOwner: "projectbluefin/review" },
							labels: { nodes: [{ name: "launcher" }] },
							commits: { nodes: [{ commit: {
								statusCheckRollup: null,
								checkSuites: { pageInfo: { hasNextPage: false }, nodes: [{ status: "COMPLETED", conclusion: "FAILURE" }] },
							} }] },
						}
						: key === "projectbluefin/other#7"
							? {
								number: 7,
								title: "feat(ui): dagger rail",
								url: "https://github.com/projectbluefin/other/pull/7",
								updatedAt: new Date(NOW - 5000).toISOString(),
								isDraft: false,
								mergeable: "MERGEABLE",
								reviewDecision: "APPROVED",
								headRefOid: "7".repeat(40),
								author: { login: "ada" },
								repository: { nameWithOwner: "projectbluefin/other" },
								labels: { nodes: [] },
								commits: { nodes: [{ commit: { statusCheckRollup: { state: "SUCCESS" } } }] },
							}
							: null;
					data[name] = { issueOrPullRequest: Object.hasOwn(known, key) ? known[key] : fallback };
				}
				return { ok: true, status: 200, statusText: "OK", json: async () => ({ data }) };
			}
			assert.match(body.variables.search, /org:projectbluefin/);
			if (body.variables.search.includes("is:pr")) assert.match(body.query, /checkSuites/);
			return {
				ok: true,
				status: 200,
				statusText: "OK",
				json: async () => ({
					data: {
						viewer: { login: "jorge" },
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
									headRefOid: "4".repeat(40),
									repository: { nameWithOwner: "projectbluefin/review" },
									labels: { nodes: [{ name: "launcher" }] },
									commits: { nodes: [{ commit: {
										statusCheckRollup: null,
										checkSuites: { pageInfo: { hasNextPage: false }, nodes: [{ status: "COMPLETED", conclusion: "FAILURE" }] },
									} }] },
								},
								{
									number: 7,
									title: "feat(ui): dagger rail",
									url: "https://github.com/projectbluefin/other/pull/7",
									updatedAt: new Date(NOW - 5000).toISOString(),
									isDraft: false,
									mergeable: "MERGEABLE",
									reviewDecision: "APPROVED",
									changedFiles: 2,
									author: { login: "ada" },
									headRefOid: "7".repeat(40),
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

function hiveBackedFetch(items, calls = []) {
	const nodes = items.map((item) => ({
		number: item.id,
		title: item.title,
		url: `https://github.com/${item.repo}/pull/${item.id}`,
		updatedAt: new Date(NOW - item.id * 1000).toISOString(),
		isDraft: false,
		mergeable: "MERGEABLE",
		reviewDecision: "REVIEW_REQUIRED",
		headRefOid: item.headSha ?? String(item.id).padStart(40, "0"),
		changedFiles: item.changedFiles,
		autoMergeRequest: item.autoMergeEnabled ? { enabledAt: new Date(NOW).toISOString() } : null,
		author: { login: "reviewer" },
		repository: { nameWithOwner: item.repo },
		labels: { nodes: [] },
		commits: { nodes: [{ commit: { statusCheckRollup: { state: (item.ciStatus ?? "success").toUpperCase() } } }] },
	}));
	return async (url, init) => {
		const target = String(url);
		calls.push(target);
		if (target.endsWith("/api/v1/status")) {
			return { ok: true, status: 200, statusText: "OK", json: async () => ({ actionable_items: items.length }) };
		}
		if (target.endsWith("/api/contribute/queue")) {
			return {
				ok: true,
				status: 200,
				statusText: "OK",
				json: async () => ({ queue: items.map((item) => ({ key: `${item.repo}#${item.id}`, repo: item.repo, number: item.id, title: item.title })) }),
			};
		}
		if (target.endsWith("/api/contribute/triage")) {
			return { ok: true, status: 200, statusText: "OK", json: async () => ({ groups: [] }) };
		}
		if (target.endsWith("/api/v1/contributors")) {
			return { ok: true, status: 200, statusText: "OK", json: async () => ({ contributors: [] }) };
		}
		if (target.includes("/graphql")) {
			const body = JSON.parse(String(init?.body ?? "{}"));
			if (body.variables?.search !== undefined) {
				return { ok: true, status: 200, statusText: "OK", json: async () => ({ data: { search: { pageInfo: { hasNextPage: false, endCursor: null }, nodes } } }) };
			}
			const data = {};
			const aliases = /(\w+): repository\(owner: "([^"]+)", name: "([^"]+)"\)\s*\{\s*issueOrPullRequest\(number: (\d+)\)/g;
			for (const [, alias, owner, repo, number] of body.query.matchAll(aliases)) {
				data[alias] = { issueOrPullRequest: nodes.find((node) => node.number === Number(number) && node.repository.nameWithOwner === `${owner}/${repo}`) ?? null };
			}
			return { ok: true, status: 200, statusText: "OK", json: async () => ({ data }) };
		}
		return { ok: true, status: 200, statusText: "OK", json: async () => [] };
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
		execCalls: [],
		execResult: { stdout: "https://github.com/projectbluefin/review/issues/42#issuecomment-1\n", stderr: "", code: 0, killed: false },
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
		async exec(command, args) {
			this.execCalls.push({ command, args });
			return this.execResult;
		},
		appendEntry(customType, data) {
			this.entries.push({ customType, data });
		},
	};
}

/**
 * Fake omp session context.
 *
 * `ui.custom` models the real workbench: it builds the component and resolves
 * only when that component calls `done`. Resolving immediately would hide a
 * session-start handler that blocks OMP.
 */
function fakeCtx() {
	const notifications = [];
	const statuses = new Map();
	const widgets = new Map();
	const overlays = [];
	const pasted = [];
	const confirmations = [];
	const editorResponses = [];
	const ctx = {
		hasUI: true,
		notifications,
		statuses,
		widgets,
		overlays,
		pasted,
		confirmations,
		editorResponses,
		asyncJobs: { running: [], recent: [], delivery: { pending: 0 } },
		ui: {
			notify: (message, level) => notifications.push({ message, level }),
			confirm: async (title, message) => {
				confirmations.push({ title, message });
				return true;
			},
			editor: async () => editorResponses.shift(),
			setStatus: (key, value) => statuses.set(key, value),
			setWidget: (key, content) => widgets.set(key, content),
			setTitle: () => {},
			pasteToEditor(text) {
				pasted.push(text);
			},
			custom(factory) {
				const { promise, resolve } = Promise.withResolvers();
				overlays.push(factory({ requestRender: () => {} }, this.theme, {}, resolve));
				return promise;
			},
			theme: { fg: (_c, t) => t, bold: (t) => t, inverse: (t) => t },
		},
		getAsyncJobSnapshot() {
			return this.asyncJobs;
		},
		sessionManager: { getBranch: () => [] },
	};
	return ctx;
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
test("workbench palette changes every semantic accent with issue mode", () => {
	let mode: "prs" | "issues" = "prs";
	const theme = {
		fg: (color: string, text: string) => `<${color}>${text}`,
		bold: (text: string) => text,
		inverse: (text: string) => text,
	};
	const painter = workbenchPainter(theme, () => mode);
	assert.equal(painter.fg("accent", "mode"), "<accent>mode");
	mode = "issues";
	assert.equal(painter.fg("accent", "mode"), "<warning>mode");
	assert.equal(painter.fg("border", "─"), "<warning>─");
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


test("the queue badge comes directly from GitHub checks", () => {
	const item = queueItem({ ciStatus: "failure" });
	assert.equal(ciGlyph(item.ciStatus).status, "failure");
	assert.equal(queueItem({ ciStatus: "success" }).ciStatus, "success");
});

test("a cancelled tool call is skipped, not a failure (#465)", () => {
	const trace = new SessionTrace();
	trace.startTurn(NOW);
	trace.startTool("t1", "bash", "rm -rf /", NOW);
	// The queue moved on: omp surfaces cancellation as a message in the result.
	trace.endTool("t1", "tool call cancelled, no longer needed", true, NOW);
	trace.endTurn(NOW);
	const run = trace.roots();
	assert.equal(run[0].status, "skipped");
	assert.equal(run[0].cls, "cancelled");
	const text = traceToText(run, NOW, 200);
	assert.match(text, /CANCELLED/);
	assert.doesNotMatch(text, /\u2718/); // no red ✘ failure glyph
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
	assert.equal(ok.viewerLogin, "jorge");

	const missing = await fetchQueue("prs", { fetchImpl: fakeFetch([]) });
	assert.match(missing.error ?? "", /no GitHub credential/);

	const denied = await fetchQueue("prs", {
		token: "t",
		fetchImpl: async () => ({ ok: false, status: 401, statusText: "Unauthorized", json: async () => ({}) }),
	});
	assert.match(denied.error ?? "", /401/, "a failed queue must say why, not render empty");
});

test("check suites surface failures and pending runs without rollup contexts", async () => {
	const fetchImpl = async (_url, init) => {
		const body = JSON.parse(String(init?.body ?? "{}"));
		assert.match(body.query, /checkSuites\(first: 50\)/);
		const node = (number, checkSuites) => ({
			number,
			title: `suite ${number}`,
			url: `https://github.com/projectbluefin/review/pull/${number}`,
			updatedAt: new Date(NOW).toISOString(),
			repository: { nameWithOwner: "projectbluefin/review" },
			commits: { nodes: [{ commit: { statusCheckRollup: null, checkSuites } }] },
		});
		return {
			ok: true,
			status: 200,
			statusText: "OK",
			json: async () => ({ data: { search: { pageInfo: { hasNextPage: false, endCursor: null }, nodes: [
				node(1, { pageInfo: { hasNextPage: false }, nodes: [
					{ status: "COMPLETED", conclusion: "FAILURE" },
					{ status: "IN_PROGRESS", conclusion: null },
				] }),
				node(2, { pageInfo: { hasNextPage: false }, nodes: [{ status: "IN_PROGRESS", conclusion: null }] }),
				node(3, { pageInfo: { hasNextPage: false }, nodes: [
					{ status: "COMPLETED", conclusion: "SUCCESS" },
					{ status: "COMPLETED", conclusion: "NEUTRAL" },
					{ status: "COMPLETED", conclusion: "SKIPPED" },
				] }),
			] } } }),
		};
	};
	const result = await fetchQueue("prs", { token: "t", fetchImpl });
	assert.deepEqual(result.items.map((item) => item.ciStatus), ["failure", "pending", "success"]);
});

test("successful statusCheckRollup takes precedence over unrelated queued check suites (#592)", async () => {
	const fetchImpl = async (_url, init) => {
		const node = (number, statusCheckRollup, checkSuites) => ({
			number,
			title: `suite ${number}`,
			url: `https://github.com/projectbluefin/review/pull/${number}`,
			updatedAt: new Date(NOW).toISOString(),
			repository: { nameWithOwner: "projectbluefin/review" },
			commits: { nodes: [{ commit: { statusCheckRollup, checkSuites } }] },
		});
		return {
			ok: true,
			status: 200,
			statusText: "OK",
			json: async () => ({
				data: {
					search: {
						pageInfo: { hasNextPage: false, endCursor: null },
						nodes: [
							node(
								1,
								{ state: "SUCCESS" },
								{
									pageInfo: { hasNextPage: false },
									nodes: [
										{ app: { name: "Azure Boards" }, status: "QUEUED", conclusion: null },
										{ app: { name: "Veracode Workflow App" }, status: "QUEUED", conclusion: null },
									],
								},
							),
						],
					},
				},
			}),
		};
	};
	const result = await fetchQueue("prs", { token: "t", fetchImpl });
	assert.equal(result.items[0].ciStatus, "success");
});

test("toCiStatus prioritizes decisive rollup and falls back to check suites (#592)", () => {
	// Rollup precedence over check suites
	const queuedSuites = {
		pageInfo: { hasNextPage: false },
		nodes: [
			{ status: "QUEUED", conclusion: null },
			{ status: "QUEUED", conclusion: null },
		],
	};
	assert.equal(toCiStatus("SUCCESS", queuedSuites), "success");
	assert.equal(toCiStatus("FAILURE", queuedSuites), "failure");
	assert.equal(toCiStatus("ERROR", queuedSuites), "failure");
	assert.equal(toCiStatus("PENDING", queuedSuites), "pending");

	// Fallback when rollup is absent or indeterminate
	assert.equal(toCiStatus(undefined, queuedSuites), "pending");
	assert.equal(toCiStatus(undefined, { pageInfo: { hasNextPage: true }, nodes: [] }), "pending");
	assert.equal(toCiStatus(undefined, {
		pageInfo: { hasNextPage: false },
		nodes: [{ status: "COMPLETED", conclusion: "FAILURE" }],
	}), "failure");
	assert.equal(toCiStatus(undefined, {
		pageInfo: { hasNextPage: false },
		nodes: [{ status: "COMPLETED", conclusion: "SUCCESS" }],
	}), "success");
	assert.equal(toCiStatus(undefined, {
		pageInfo: { hasNextPage: false },
		nodes: [
			{ status: "COMPLETED", conclusion: "SUCCESS" },
			{ status: "COMPLETED", conclusion: "NEUTRAL" },
			{ status: "COMPLETED", conclusion: "SKIPPED" },
		],
	}), "success");
	assert.equal(toCiStatus(undefined, null), undefined);
	assert.equal(toCiStatus(undefined, { pageInfo: { hasNextPage: false }, nodes: [] }), undefined);
});

test("pull request queue keeps workflow changes and incomplete file lists visible but blocked", async () => {
	const fetchImpl = async (_url, init) => {
		const body = JSON.parse(String(init?.body ?? "{}"));
		assert.match(body.query, /files\(first: 100\)/);
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
								number: 1,
								title: "workflow change",
								url: "https://github.com/projectbluefin/review/pull/1",
								updatedAt: new Date(NOW).toISOString(),
								repository: { nameWithOwner: "projectbluefin/review" },
								changedFiles: 1,
								files: { pageInfo: { hasNextPage: false }, nodes: [{ path: ".github/workflows/validate.yml" }] },
							},
							{
								number: 2,
								title: "ordinary change",
								url: "https://github.com/projectbluefin/review/pull/2",
								updatedAt: new Date(NOW).toISOString(),
								repository: { nameWithOwner: "projectbluefin/review" },
								changedFiles: 1,
								files: { pageInfo: { hasNextPage: false }, nodes: [{ path: "README.md" }] },
							},
							{
								number: 3,
								title: "truncated file list",
								url: "https://github.com/projectbluefin/review/pull/3",
								updatedAt: new Date(NOW).toISOString(),
								repository: { nameWithOwner: "projectbluefin/review" },
								changedFiles: 150,
								files: { pageInfo: { hasNextPage: true }, nodes: [{ path: "README.md" }] },
							},
						],
					},
				},
			}),
		};
	};
	const mode = new ReviewMode({ org: "projectbluefin", fetchImpl, env: ISOLATED_ENV });
	mode.setToken("t");
	await mode.refreshQueue();

	assert.deepEqual(
		mode.items.map((item) => `${item.repo}#${item.id}`),
		["projectbluefin/review#1", "projectbluefin/review#2", "projectbluefin/review#3"],
	);
	assert.equal(mode.position(), "1/3");

	assert.equal(mode.selectById("projectbluefin/review", 1), true);
	const pr1 = mode.selected()!;
	const priority1 = mode.priorityFor(pr1);
	assert.equal(priority1?.category, "blocked");
	assert.equal(priority1?.reason, "workflow change");
	assert.match(priorityChip(PLAIN_PAINTER, priority1), /blocked · workflow change/);

	assert.equal(mode.selectById("projectbluefin/review", 2), true);
	const pr2 = mode.selected()!;
	assert.notEqual(mode.priorityFor(pr2)?.category, "blocked");

	assert.equal(mode.selectById("projectbluefin/review", 3), true);
	const pr3 = mode.selected()!;
	const priority3 = mode.priorityFor(pr3);
	assert.equal(priority3?.category, "blocked");
	assert.equal(priority3?.reason, "incomplete changed-file list");
	assert.match(priorityChip(PLAIN_PAINTER, priority3), /blocked · incomplete changed-file list/);

	// A queue containing only unsupported PRs does not say 0/0 or "nothing open"
	const unsupportedOnlyFetch = async () => ({
		ok: true,
		status: 200,
		statusText: "OK",
		json: async () => ({
			data: {
				search: {
					pageInfo: { hasNextPage: false, endCursor: null },
					nodes: [
						{
							number: 956,
							title: "build and deploy workflows",
							url: "https://github.com/projectbluefin/review/pull/956",
							updatedAt: new Date(NOW).toISOString(),
							repository: { nameWithOwner: "projectbluefin/review" },
							changedFiles: 3,
							files: { pageInfo: { hasNextPage: false }, nodes: [{ path: ".github/workflows/deploy.yml" }] },
						},
					],
				},
			},
		}),
	});
	const unsupportedMode = new ReviewMode({ org: "projectbluefin", fetchImpl: unsupportedOnlyFetch, env: ISOLATED_ENV });
	unsupportedMode.setToken("t");
	await unsupportedMode.refreshQueue();

	assert.equal(unsupportedMode.items.length, 1);
	assert.equal(unsupportedMode.position(), "1/1");
	const dashboard = new ReviewDashboard({ requestRender() {} }, PLAIN_PAINTER, unsupportedMode, () => {}, () => {}, 24);
	const rendered = dashboard.render(80).join("\n");
	assert.doesNotMatch(rendered, /nothing open/);
	assert.doesNotMatch(rendered, /0\/0/);
	assert.match(rendered, /#956/);
	assert.match(rendered, /blocked · workflow change/);
	assert.match(rendered, /BLOCKED · workflow change/);
	assert.match(rendered, /changes \.github\/workflows\/deploy\.yml/);
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

	const mode = new ReviewMode({ org: "projectbluefin", fetchImpl,
		env: ISOLATED_ENV, });
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

	const mode = new ReviewMode({ org: "projectbluefin", fetchImpl,
		env: ISOLATED_ENV, });
	mode.setToken("t");
	mode.setScope({ kind: "repo", value: "owner/old" });
	const oldKey = "owner/old#9";
	mode.hive = {
		...EMPTY_HIVE,
		configured: true,
		online: true,
		hub: "https://hive.example",
		items: [{ key: oldKey, repo: "owner/old", number: 9, title: "old", url: "https://github.com/owner/old/pull/9", labels: [] }],
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



test("Hive coverage counts only work matching the active queue mode", () => {
	const mode = new ReviewMode({ org: "projectbluefin", env: ISOLATED_ENV });
	mode.hive = {
		...EMPTY_HIVE,
		configured: true,
		online: true,
		hub: "https://hive.example",
		items: [
			{ key: "projectbluefin/review#1", repo: "projectbluefin/review", number: 1, title: "issue", url: "https://github.com/projectbluefin/review/issues/1", labels: [] },
			{ key: "projectbluefin/review#2", repo: "projectbluefin/review", number: 2, title: "pull", url: "https://github.com/projectbluefin/review/pull/2", labels: [] },
			{ key: "projectbluefin/review#3", repo: "projectbluefin/review", number: 3, title: "linked", url: "https://github.com/projectbluefin/review/issues/3", labels: [], pr: { number: 4, url: "https://github.com/projectbluefin/review/pull/4", state: "open" } },
			{ key: "projectbluefin/review#5", repo: "projectbluefin/review", number: 5, title: "merged", url: "https://github.com/projectbluefin/review/issues/5", labels: [], pr: { number: 6, url: "https://github.com/projectbluefin/review/pull/6", state: "merged" } },
		],
		ranks: new Map([
			["projectbluefin/review#1", 0],
			["projectbluefin/review#2", 1],
			["projectbluefin/review#3", 2],
			["projectbluefin/review#5", 3],
		]),
	};

	mode.queueMode = "prs";
	mode.items = [queueItem({ id: 4, closingIssues: ["projectbluefin/review#3"] })];
	mode.reprioritize();
	assert.deepEqual(mode.hiveCoverage(), { present: 1, total: 2 });

	mode.queueMode = "issues";
	mode.items = [
		queueItem({ id: 1, type: "issue" }),
		queueItem({ id: 3, type: "issue" }),
		queueItem({ id: 5, type: "issue" }),
	];
	mode.reprioritize();
	assert.deepEqual(mode.hiveCoverage(), { present: 3, total: 3 });
});
test("issue queue fetch maps merged closedByPullRequestsReferences into closedByPrs", async () => {
	const issueFetch = async () => ({
		ok: true,
		status: 200,
		statusText: "OK",
		json: async () => ({
			data: {
				search: {
					pageInfo: { hasNextPage: false, endCursor: null },
					nodes: [
						{
							number: 1130,
							title: "Cache Maintenance fails on jq syntax error",
							url: "https://github.com/projectbluefin/bluefin/issues/1130",
							updatedAt: new Date(NOW - 1000).toISOString(),
							author: { login: "hive" },
							repository: { nameWithOwner: "projectbluefin/bluefin" },
							labels: { nodes: [] },
							closedByPullRequestsReferences: {
								nodes: [
									{
										number: 1203,
										state: "MERGED",
										merged: true,
										repository: { nameWithOwner: "projectbluefin/bluefin" },
									},
									{
										number: 1204,
										state: "OPEN",
										merged: false,
										repository: { nameWithOwner: "projectbluefin/bluefin" },
									},
								],
							},
						},
					],
				},
			},
		}),
	});
	const res = await fetchQueue("issues", { token: "t", fetchImpl: issueFetch });
	assert.equal(res.items.length, 1);
	assert.deepEqual(res.items[0].closedByPrs, ["projectbluefin/bluefin#1203"]);
	assert.deepEqual(res.items[0].submittedPrs, ["projectbluefin/bluefin#1203", "projectbluefin/bluefin#1204"]);
});

test("diff fetch is bounded but honest about it", async () => {
	const diff = await fetchDiff("projectbluefin/review", 42, { token: "t", fetchImpl: fakeFetch([]), maxPatchChars: 100 });
	assert.equal(diff.totalFiles, 2);
	assert.equal(diff.additions, 903);
	assert.ok(diff.files[0].patch, "the first patch is included");
	assert.ok(diff.files[1].patch === undefined || diff.files[1].patch.includes("truncated"));
	assert.equal(diff.truncated, true);
});

test("exactHeadVerified refuses unless the workspace is the exact expected head", () => {
	const sha = "a".repeat(40);
	assert.equal(exactHeadVerified(sha, sha), true, "an exact head match proceeds");
	assert.equal(exactHeadVerified(sha, "b".repeat(40)), false, "a different head is refused");
	assert.equal(exactHeadVerified(sha, undefined), false, "an unreadable workspace is refused");
	assert.equal(exactHeadVerified(undefined, sha), false, "a diff alone does not prove the head");
	assert.equal(exactHeadVerified(undefined, undefined), false, "nothing verifies without an expected head");
});

test("fetchDiff carries the PR head SHA so verification can materialize it exactly", async () => {
	const head = "c".repeat(40);
	const headFetch = (url: string | URL, init?: RequestInit) => {
		const target = String(url);
		if (target.includes("/pulls/42/files")) return fakeFetch([])(url, init);
		if (target.includes("/pulls/42")) {
			return { ok: true, status: 200, statusText: "OK", json: async () => ({ head: { sha: head } }) };
		}
		return fakeFetch([])(url, init);
	};
	const diff = await fetchDiff("projectbluefin/review", 42, { token: "t", fetchImpl: headFetch });
	assert.equal(diff.headSha, head, "the diff reports the exact PR head");
	assert.equal(diff.totalFiles, 2);
	assert.ok(exactHeadVerified(diff.headSha, head), "the reported head verifies against itself");
});

test("#440 a stale or unknown workspace head is refused before executable verification", async () => {
	// A diff proves only what GitHub reports. If the materialized workspace is not
	// the diff's exact head, verification must refuse rather than run against drift.
	const diff = await fetchDiff("projectbluefin/review", 42, { token: "t", fetchImpl: fakeFetch([]) });
	assert.equal(diff.headSha, null, "no PR payload means an unknown head");
	assert.equal(exactHeadVerified(diff.headSha, "a".repeat(40)), false, "an unknown expected head refuses");

	const head = "d".repeat(40);
	assert.equal(exactHeadVerified(head, head), true, "a matching workspace proceeds");
	assert.equal(exactHeadVerified(head, "e".repeat(40)), false, "a stale workspace head is refused");
});

test("the diff tool surfaces the exact head and status shows expected vs workspace SHA", async () => {
	const head = "4".repeat(40);
	const headFetch = (url: string | URL, init?: RequestInit) => {
		const target = String(url);
		if (target.includes("/pulls/42/files")) return fakeFetch([])(url, init);
		if (target.includes("/pulls/42")) {
			return { ok: true, status: 200, statusText: "OK", json: async () => ({ head: { sha: head } }) };
		}
		return fakeFetch([])(url, init);
	};
	const pi = fakeHost();
	const review = createReviewExtension(pi, { org: "projectbluefin", fetchImpl: headFetch, env: ISOLATED_ENV });
	const ctx = fakeCtx();
	ctx.ui.parent = ctx;
	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();

	const diff = await pi.tools.get("hive_workbench_diff").execute("id", { pull_request: 42 });
	assert.equal(diff.details.head_sha, head, "the diff tool surfaces the exact PR head");

	const status = await pi.tools.get("hive_workbench_status").execute("id", {});
	assert.match(status.content[0].text, /head: 4/, "status shows the expected head SHA");
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

	const mode = new ReviewMode({ org: "projectbluefin", fetchImpl: paged });
	mode.setToken("t");
	await mode.refreshQueue();
	assert.match(mode.position(), /\+$/);

	const complete = await fetchQueue("prs", { token: "t", fetchImpl: fakeFetch([]) });
	assert.equal(complete.truncated, false);
});

test("without Hive the queue stays in GitHub evidence order and remains descriptive", async () => {
	const mode = new ReviewMode({ org: "projectbluefin", fetchImpl: fakeFetch([]), env: ISOLATED_ENV });
	mode.setToken("t");
	await mode.refreshQueue();
	assert.equal(mode.visibleItems().length, 2);

	assert.equal(mode.selected().id, 42, "unreachable-Hive mode preserves the fetched GitHub order");
	assert.equal(mode.priorityFor(mode.selected()).category, "fix-ci", "categories remain descriptive");
	assert.equal(mode.orderSource(), "local");

	mode.move(1);
	assert.equal(mode.selected().id, 7);
	assert.equal(mode.priorityFor(mode.selected()).category, "ready-for-human-merge");
	const before = mode.selectedKey();
	await mode.refreshQueue();
	assert.equal(mode.selectedKey(), before, "a refetch must not move the cursor off the item you were reading");

	mode.setFilter("launcher");
	assert.equal(mode.visibleItems().length, 1);
	assert.equal(mode.selected().id, 42);
	mode.setFilter("fix-ci");
	assert.equal(mode.selected().id, 42, "the category is part of the filter surface");
	mode.setFilter("");
	mode.skipRepos.add("other");
	assert.equal(mode.visibleItems().length, 1);
	assert.equal(mode.selected().id, 42, "skipped repo items are filtered out");
	mode.skipRepos.clear();
	assert.equal(mode.visibleItems().length, 2);

	assert.deepEqual(mode.ciTally(), { success: 1, failure: 1, pending: 0, unknown: 0 });
});

test("pull requests returned to their authenticated author precede Hive-ranked review work", () => {
	const returned = queueItem({ id: 9, author: "jorge", reviewState: "changes_requested", ciStatus: "failure" });
	const firstHive = queueItem({ id: 1, author: "ada", reviewState: "review_required", ciStatus: "success" });
	const secondHive = queueItem({ id: 2, author: "grace", reviewState: "review_required", ciStatus: "success" });
	const hive = {
		...EMPTY_HIVE,
		online: true,
		configured: true,
		ranks: new Map([
			[`${firstHive.repo}#${firstHive.id}`, 0],
			[`${secondHive.repo}#${secondHive.id}`, 1],
		]),
	};
	const ranked = prioritize([firstHive, secondHive, returned], { hive, now: NOW, currentUserLogin: "jorge" });

	assert.deepEqual(ranked.items.map((item) => item.id), [9, 1, 2]);
	assert.equal(ranked.priorities.get(`${returned.repo}#${returned.id}`)?.category, "repair-requested");
	assert.match(priorityChip(PLAIN_PAINTER, ranked.priorities.get(`${returned.repo}#${returned.id}`)), /repair/);
});

test("rail renders the queue, OMP activity, and keymap within width", () => {
	const mode = new ReviewMode({ org: "projectbluefin" });
	mode.items = [queueItem()];
	mode.fetchedAt = NOW - 3000;

	const rows = renderRail(mode, PLAIN_PAINTER, 100, NOW, 0, [{ chord: "alt+b", label: "workbench" }]);
	assert.ok(rows.some((row) => row.includes("projectbluefin/review#42") || row.includes("#42")));
	assert.ok(rows.some((row) => row.includes("alt+b: workbench")));
	for (const row of rows) assert.ok(visibleWidth(row) <= 100);

	mode.session.startTurn(NOW - 1000);
	const activeRows = renderRail(mode, PLAIN_PAINTER, 100, NOW, 0, [{ chord: "alt+b", label: "workbench" }]);
	assert.ok(activeRows.some((row) => row.includes("turn 1")), "the rail projects live OMP activity");
	for (const row of activeRows) assert.ok(visibleWidth(row) <= 100);

	assert.match(statusSegment(mode, PLAIN_PAINTER, NOW), /PR 1\/1 #42/);
	mode.selectedKeys.add("projectbluefin/review#42");
	assert.match(statusSegment(mode, PLAIN_PAINTER, NOW), /PR \[1 sel\] 1\/1 #42/);
	mode.selectedKeys.clear();

});

test("rail explains an empty queue instead of pretending to load forever", () => {
	const mode = new ReviewMode({ org: "projectbluefin" });
	mode.queueError = "GitHub GraphQL 401 Unauthorized";
	const rows = renderRail(mode, PLAIN_PAINTER, 80, NOW, 0, []);
	assert.match(rows[0], /401/);
});

test("rail and dashboard clarify an explicitly enabled empty Hive-only filter", () => {
	const mode = new ReviewMode({ org: "projectbluefin" });
	mode.hive = {
		...EMPTY_HIVE,
		configured: true,
		online: true,
		hub: "https://hive.example",
	};
	mode.items = [queueItem({ id: 10, title: "Unranked item" })];
	mode.reprioritize();
	mode.toggleHiveOnly();

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

	const mode = new ReviewMode({ org: "projectbluefin" });
	mode.items = [queueItem()];
	mode.fetchedAt = NOW - 1000;
	assert.ok(!renderRail(mode, PLAIN_PAINTER, 100, NOW, 0, [])[0].includes("stale"));
	mode.fetchedAt = NOW - STALE_AFTER_MS - 1;
	assert.match(renderRail(mode, PLAIN_PAINTER, 100, NOW, 0, [])[0], /stale/);
});


test("dashboard navigates, folds, filters, and returns actions", (t) => {
	const root = mkdtempSync(join(tmpdir(), "workbench-test-"));
	t.after(() => rmSync(root, { recursive: true, force: true }));
	const mode = new ReviewMode({ org: "projectbluefin" });
	mode.items = [queueItem(), queueItem({ id: 7, repo: "projectbluefin/other", title: "feat(ui): dagger rail", ciStatus: "success" })];
	;

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
	assert.ok(frame()[0].includes("HIVE WORKBENCH"));
	for (const row of frame()) assert.ok(visibleWidth(row) <= 120, row);

	assert.equal(mode.selected().id, 42, "unreachable-Hive mode preserves fetched order");
	dashboard.handleInput("j");
	assert.equal(mode.selected().id, 7);
	dashboard.handleInput("k");
	assert.equal(mode.selected().id, 42);

	// Trace pane: `t` changes pane; Tab is reserved for PR/issue mode.
	dashboard.handleInput("t");
	dashboard.handleInput("h");
	const folded = frame().filter((row) => row.includes("review#42")).length;
	dashboard.handleInput("l");
	assert.ok(frame().length >= folded);

	// Filtering is inline and only commits on Enter.
	dashboard.handleInput("t");
	dashboard.handleInput("/");
	for (const ch of "dagger") dashboard.handleInput(ch);
	assert.equal(mode.filter, "", "filter is a draft until committed");
	dashboard.handleInput("\r");
	assert.equal(mode.visibleItems().length, 1);
	assert.equal(mode.selected().id, 7);
	assert.equal(mode.filter, "dagger");

	dashboard.handleInput("r");
	assert.equal(refreshes, 1);

	dashboard.handleInput("s");
	assert.equal(action.kind, "slay");
	assert.equal(action.item.id, 7);

	dashboard.handleInput("\r");
	assert.equal(action.kind, "reference");
	assert.equal(action.item.id, 7);
	dashboard.handleInput("c");
	assert.equal(action.kind, "comment");

	dashboard.handleInput("o");
	assert.equal(action.kind, "scope", "o asks for another repository");
	dashboard.handleInput("q");
	assert.equal(action.kind, "close");

	dashboard.handleInput("?");
	assert.ok(frame().some((row) => row.includes("comment")), "help lists supported actions");
	assert.ok(frame().some((row) => row.includes("slay")), "help exposes mass autoreview");
});
test("dashboard interactive search live-filters and selects items by title", (t) => {
	const root = mkdtempSync(join(tmpdir(), "workbench-test-"));
	t.after(() => rmSync(root, { recursive: true, force: true }));
	const mode = new ReviewMode({ org: "projectbluefin" });
	mode.items = [
		queueItem({ id: 7, repo: "projectbluefin/other", title: "token exchange fix", ciStatus: "success" }),
		queueItem({ id: 42, repo: "projectbluefin/review", title: "second item", ciStatus: "failure" }),
	];
	;
	const dashboard = new ReviewDashboard({ requestRender() {} }, PLAIN_PAINTER, mode, () => {}, () => {}, 20);
	t.after(() => dashboard.dispose());

	// Open search input with '/'
	dashboard.handleInput("/");
	assert.ok(dashboard.render(100).some((line) => line.includes("search")));

	// Type search query for title "token"
	for (const ch of "token") dashboard.handleInput(ch);
	dashboard.handleInput("\r");

	// The rendered queue shows only the matching item
	const renderedSearch = dashboard.render(100);
	assert.ok(renderedSearch.some((line) => line.includes("#7") && line.includes("token exchange fix")));
	assert.ok(!renderedSearch.some((line) => line.includes("#42") && line.includes("second item")));

	// Select it with Tab or Space
	dashboard.handleInput(" ");
	assert.ok(mode.selectedKeys.has("projectbluefin/other#7"), "selected item via search input");
	assert.equal(mode.selectedKeys.size, 1);
	assert.ok(mode.selectedKeys.has("projectbluefin/other#7"));
});

test("workbench Tab switches entity mode and Alt+B selects one repository group", (t) => {
	const root = mkdtempSync(join(tmpdir(), "workbench-test-"));
	t.after(() => rmSync(root, { recursive: true, force: true }));
	const mode = new ReviewMode({ org: "projectbluefin" });
	mode.items = [
		queueItem({ id: 1, repo: "projectbluefin/a", ciStatus: "success" }),
		queueItem({ id: 2, repo: "projectbluefin/a", ciStatus: "success" }),
		queueItem({ id: 3, repo: "projectbluefin/b", ciStatus: "failure" }),
	];
	mode.reprioritize();
	mode.selectById("projectbluefin/a", 1);
	const modes: string[] = [];
	let refreshes = 0;
	const dashboard = new ReviewDashboard(
		{ requestRender() {} },
		PLAIN_PAINTER,
		mode,
		() => {},
		() => { refreshes += 1; },
		20,
		undefined,
		(next) => modes.push(next),
	);
	t.after(() => dashboard.dispose());

	dashboard.handleInput("alt+b");
	assert.deepEqual([...mode.selectedKeys].sort(), ["projectbluefin/a#1", "projectbluefin/a#2"]);
	dashboard.handleInput("alt+b");
	assert.equal(mode.selectedKeys.size, 0);

	dashboard.handleInput("\t");
	assert.equal(mode.queueMode, "issues");
	assert.deepEqual(modes, ["issues"]);
	assert.equal(refreshes, 1);
	assert.match(dashboard.render(120).join("\n"), /HIVE WORKBENCH/);
});

test("dashboard Alt-S requests the repair-first autoslay lifecycle", (t) => {
	const root = mkdtempSync(join(tmpdir(), "workbench-test-"));
	t.after(() => rmSync(root, { recursive: true, force: true }));
	const mode = new ReviewMode({ org: "projectbluefin" });
	mode.items = [queueItem({ id: 1, repo: "projectbluefin/a", ciStatus: "success" })];
	mode.reprioritize();
	let emittedAction: DashboardAction | undefined;
	const dashboard = new ReviewDashboard(
		{ requestRender() {} },
		PLAIN_PAINTER,
		mode,
		(action) => { emittedAction = action; },
		() => {},
		20,
	);
	t.after(() => dashboard.dispose());

	dashboard.handleInput("alt+s");
	assert.deepEqual(emittedAction, { kind: "autoslay" });
	emittedAction = undefined;
	dashboard.handleInput("\u001bs");
	assert.deepEqual(emittedAction, { kind: "autoslay" });
});

test("repository waves preserve interleaved Hive order and pause survives session persistence", () => {
	const mode = new ReviewMode({ org: "projectbluefin" });
	mode.items = [
		queueItem({ id: 1, repo: "projectbluefin/a" }),
		queueItem({ id: 3, repo: "projectbluefin/b" }),
		queueItem({ id: 2, repo: "projectbluefin/a" }),
	];
	mode.reprioritize();
	for (const item of mode.items) mode.selectedKeys.add(`${item.repo}#${item.id}`);
	assert.deepEqual(
		mode.repositoryWaves().map((wave) => [wave.repo, wave.items.map((item) => item.id)]),
		[["projectbluefin/a", [1]], ["projectbluefin/b", [3]], ["projectbluefin/a", [2]]],
	);
	assert.equal(mode.togglePaused(), true);
	const persisted = mode.toPersisted();
	assert.equal(persisted.paused, true);
	const restored = new ReviewMode({ org: "projectbluefin" });
	restored.restore(persisted);
	assert.equal(restored.paused, true);
});

test("repository waves separate repair and issue work and cap each workflowz batch", () => {
	const mode = new ReviewMode({ org: "projectbluefin" });
	mode.currentUserLogin = "jorge";
	const repairs = [queueItem({ id: 1, author: "jorge", reviewState: "changes_requested" })];
	const issues = Array.from({ length: BATCH_LIMIT + 1 }, (_, index) => queueItem({
		id: index + 100,
		type: "issue",
		reviewState: "unknown",
	}));
	const waves = mode.repositoryWaves([...repairs, ...issues]);

	assert.deepEqual(waves.map((wave) => wave.items.length), [1, BATCH_LIMIT, 1]);
	assert.deepEqual(waves.map((wave) => wave.items[0].type), ["pr", "issue", "issue"]);
});

test("comment plans bind ordered targets and fail closed on live drift", () => {
	const targets = [
		{ repo: "projectbluefin/review", number: 42, type: "pull_request" as const, headSha: "a".repeat(40) },
		{ repo: "projectbluefin/review", number: 43, type: "issue" as const },
	];
	const plan = createCommentActionPlan(targets, "Evidence-backed comment", 123);
	assert.equal(plan.createdAt, 123);
	assert.match(renderCommentActionPlan(plan), /projectbluefin\/review#42/);
	assert.match(renderCommentActionPlan(plan), /Evidence-backed comment/);
	assert.equal(validateCommentActionPlan(plan, targets).valid, true);
	assert.equal(
		validateCommentActionPlan(plan, [{ ...targets[0], headSha: "b".repeat(40) }, targets[1]]).valid,
		false,
	);
	assert.deepEqual(commentInvocation(targets[1], plan.body), {
		command: "gh",
		args: ["issue", "comment", "43", "--repo", "projectbluefin/review", "--body", "Evidence-backed comment"],
	});
	assert.throws(() => createCommentActionPlan(targets, "   "), /comment body/i);
});

test("comment plans reject pull requests without a preview head", () => {
	const target = { repo: "projectbluefin/review", number: 42, type: "pull_request" as const };
	assert.throws(() => createCommentActionPlan([target], "Review evidence"), /Missing pull request head/);
	const plan = createCommentActionPlan([{ ...target, headSha: "a".repeat(40) }], "Review evidence");
	assert.equal(validateCommentActionPlan(plan, [target]).valid, false);
	assert.equal(validateCommentActionPlan({ ...plan, targets: [target] }, [target]).valid, false);
});
test("dashboard supports multi-selection with space, x to clear, and slay dispatch", (t) => {
	const root = mkdtempSync(join(tmpdir(), "workbench-test-"));
	t.after(() => rmSync(root, { recursive: true, force: true }));
	const mode = new ReviewMode({ org: "projectbluefin" });
	mode.items = [
		queueItem({ id: 7, repo: "projectbluefin/other", title: "first item", ciStatus: "success" }),
		queueItem({ id: 42, repo: "projectbluefin/review", title: "second item", ciStatus: "failure" }),
	];
	;

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
	// Slay sends every selected item through mass autoreview.
	dashboard.handleInput("s");
	assert.equal(action.kind, "slay");
	assert.equal(action.items.length, 2);
	assert.equal(action.items[0].id, 7);
	assert.equal(action.items[1].id, 42);
	// Clear with x
	dashboard.handleInput("x");
	assert.ok(!dashboard.render(120).some((row) => row.includes("selected)")));
});

test("dashboard stacks panes on a narrow terminal", (t) => {
	const mode = new ReviewMode({ org: "projectbluefin" });
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

test("without a hub the queue remains unranked in fetched order", () => {
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
	const ranked = prioritize(items, { hive: EMPTY_HIVE, now });

	assert.equal(ranked.source, "local");
	assert.deepEqual(ranked.items.map((item) => item.id), [1, 2, 3, 4, 5, 6, 7]);
	assert.equal(ranked.priorities.get("projectbluefin/review#3").category, "ready-for-human-merge");
	assert.equal(ranked.priorities.get("projectbluefin/review#7").category, "review");
	assert.match(ranked.priorities.get("projectbluefin/review#7").reason, /awaiting review/);
	assert.equal(ranked.priorities.get("projectbluefin/review#5").category, "resolve-conflicts");
	assert.equal(ranked.priorities.get("projectbluefin/review#4").category, "fix-ci");
	assert.equal(ranked.priorities.get("projectbluefin/review#2").category, "investigate");
	assert.equal(ranked.priorities.get("projectbluefin/review#1").demotion, 1, "descriptive metadata may still mark a dependency bump");
	assert.equal(categorize(queueItem({ type: "issue" }), { hive: EMPTY_HIVE, now }).category, "triage");
});
test("unreachable-Hive issue queues preserve fetched order and delineate repository transitions", () => {
	const now = NOW;
	const items = [
		queueItem({ id: 201, type: "issue", repo: "projectbluefin/server", title: "server issue", updatedAt: now - 100 }),
		queueItem({ id: 101, type: "issue", repo: "projectbluefin/actions", title: "actions issue", updatedAt: now - 50 }),
		queueItem({ id: 202, type: "issue", repo: "projectbluefin/server", title: "another server issue", updatedAt: now - 10 }),
		queueItem({ id: 102, type: "issue", repo: "projectbluefin/actions", title: "second actions issue", updatedAt: now - 20 }),
	];
	const ranked = prioritize(items, { hive: EMPTY_HIVE, now });
	assert.deepEqual(
		ranked.items.map((item) => `${item.repo}#${item.id}`),
		["projectbluefin/server#201", "projectbluefin/actions#101", "projectbluefin/server#202", "projectbluefin/actions#102"],
	);

	const mode = new ReviewMode({ org: "projectbluefin" });
	mode.items = items;
	mode.queueMode = "issues";
	mode.reprioritize();

	const dashboard = new ReviewDashboard({ requestRender() {} }, PLAIN_PAINTER, mode, () => {}, () => {}, 24);
	const lines = dashboard.render(80);
	dashboard.dispose();

	// Divider row between projectbluefin/actions and projectbluefin/server
	assert.ok(lines.some((line) => line.includes("─── projectbluefin/server")), "dashboard delineates repo transitions with horizontal dividers");
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
	const ranked = prioritize(items, { hive, now: NOW });

	assert.equal(ranked.source, "hive");
	assert.equal(ranked.hiveRanked, 2);
	assert.deepEqual(ranked.items.map((item) => item.id), [11, 5, 3], "Hive's positions are preserved, not recomputed");
	assert.match(ranked.priorities.get("projectbluefin/review#11").reason, /hive ready #1 via projectbluefin\/docs#900/);
	assert.equal(ranked.priorities.get("projectbluefin/review#3").source, "local");
});
test("mode defaults to the complete queue and H narrows it to Hive-ranked work", () => {
	const hive = {
		...EMPTY_HIVE,
		configured: true,
		online: true,
		hub: "https://hive.example",
		items: [{ key: "projectbluefin/review#11", repo: "projectbluefin/review", number: 11, title: "queued", url: "", labels: [] }],
		ranks: buildRankMap([{ key: "projectbluefin/review#11", repo: "projectbluefin/review", number: 11, title: "", url: "", labels: [] }], []),
	};
	const mode = new ReviewMode({ org: "projectbluefin", env: ISOLATED_ENV });
	mode.hive = hive;
	mode.items = [
		prItem({ id: 3, ciStatus: "success" }),
		prItem({ id: 11, ciStatus: "success" }),
	];
	mode.reprioritize();

	assert.equal(mode.hiveOnly, false, "default view includes the review queue");
	assert.equal(mode.visibleItems().length, 2);

	mode.toggleHiveOnly();
	assert.equal(mode.hiveOnly, true);
	assert.equal(mode.visibleItems().length, 1);
	assert.equal(mode.visibleItems()[0]?.id, 11);

	mode.toggleHiveOnly();
	assert.equal(mode.hiveOnly, false);
	assert.equal(mode.visibleItems().length, 2);
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
	const mode = new ReviewMode({ org: "projectbluefin", fetchImpl: scoped, env: ISOLATED_ENV });
	mode.setToken("t");
	mode.setScope(parseScope("owner/repo", "projectbluefin"));
	await mode.refreshQueue();

	assert.equal(mode.scopeLabel(), "owner/repo");
	assert.match(searches[0], /repo:owner\/repo/);
	assert.deepEqual(mode.toPersisted().scope, { kind: "repo", value: "owner/repo" });
});

test("CLI flags forward parsed scope and preselected item to mode", async () => {
	const pi = fakeHost();
	const scopedFetch = async (url: string, init?: { body?: string }) => {
		const body = JSON.parse(String(init?.body ?? "{}"));
		if (body.variables?.search !== undefined) {
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
							],
						},
					},
				}),
			};
		}
		return { ok: true, status: 200, statusText: "OK", json: async () => ({ data: {} }) };
	};
	const review = createReviewExtension(pi, { org: "projectbluefin", fetchImpl: scopedFetch, env: ISOLATED_ENV });
	const ctx = fakeCtx();
	ctx.ui.parent = ctx;
	;
	pi.flagValues.set("repo", "projectbluefin/review");
	pi.flagValues.set("pr", "42");
	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();

	const status = await pi.tools.get("hive_workbench_status").execute("id", {});
	assert.equal(status.details.scope.kind, "repo");
	assert.equal(status.details.scope.value, "projectbluefin/review");
	assert.equal(status.details.selected.id, 42);
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

	assert.deepEqual(pi.labels, ["Hive Workbench"]);
	assert.deepEqual([...pi.shortcuts.keys()].sort(), ["alt+b", "alt+s", "alt+u"]);
	assert.deepEqual([...pi.flags.keys()].sort(), ["all", "autoslay", "issues", "pr", "repo", "skip-repo"]);
	assert.deepEqual([...pi.tools.keys()].sort(), [
		"hive_workbench_diff",
		"hive_workbench_lookup",
		"hive_workbench_queue",
		"hive_workbench_status",
		"hive_workbench_trace",
	]);
	// Reserved chords would be silently dropped by omp.
	for (const chord of pi.shortcuts.keys()) assert.match(chord, /^alt\+[a-z]$/);

	const ctx = fakeCtx();
	ctx.ui.parent = ctx;
	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();
	assert.ok(ctx.statuses.get("hive_workbench")?.includes("#42"), ctx.statuses.get("hive_workbench"));
	assert.equal(typeof ctx.widgets.get("hive-workbench-rail"), "function", "the rail is a component, not capped strings");
	const diff = await pi.tools.get("hive_workbench_diff").execute("id", { pull_request: 42 });
	assert.match(diff.content[0].text, /image\/entrypoint\.sh/);
	assert.equal(diff.details.pull_request, 42);
	assert.equal(diff.details.repo, "projectbluefin/review");
	const bad = await pi.tools.get("hive_workbench_diff").execute("id", { pull_request: 0 });
	assert.equal(bad.isError, true);

	const status = await pi.tools.get("hive_workbench_status").execute("id", {});
	assert.match(status.content[0].text, /selected projectbluefin\/review#42/);
	assert.match(status.content[0].text, /order: unranked — no hive hub configured/);
	assert.match(status.content[0].text, /priority: fix-ci/);
	assert.equal(status.details.order_source, "local");
	assert.equal(status.details.hive.configured, false);
	assert.equal(status.details.scope.value, "projectbluefin");

	const queue = await pi.tools.get("hive_workbench_queue").execute("id", {});
	assert.match(queue.content[0].text, /^\[fix-ci\] projectbluefin\/review#42/m);
	assert.equal(queue.details.order_source, "local");

	pi.events.get("tool_execution_start")({ toolCallId: "x", toolName: "grep", args: { pattern: "HIVE_HUB" } }, ctx);
	const live = await pi.tools.get("hive_workbench_status").execute("id", {});
	assert.equal(live.details.selected.id, 42);
	const lookup = await pi.tools.get("hive_workbench_lookup").execute("id", {});
	assert.match(lookup.content[0].text, /Hive hub is not configured/);
	assert.equal(ctx.overlays.length, 1, "startup opens exactly one workbench");
	await pi.shortcuts.get("alt+b").handler(ctx);
	assert.equal(ctx.overlays.length, 1, "Alt+B does not stack an already-open workbench");
	assert.equal(pi.messages.length, 0, "opening the workbench never dispatches work");
	const workbench = ctx.overlays[0];
	workbench.handleInput("j");
	workbench.handleInput("s");
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(pi.messages.length, 1, "slay dispatches without requiring Hive ranking");
	assert.match(pi.messages[0], /bluefin-reviewer/);
	assert.match(status.content[0].text, /review, fix, and slay remain available/, "a missing Hive must not read as browse-only: authorized actions remain available");
});

test("--autoslay repairs returned pull requests before implementing issue waves", async () => {
	let repairHead = "a".repeat(40);
	let issueHasPullRequest = false;
	const repairNode = () => ({
		number: 41,
		title: "address requested changes",
		url: "https://github.com/projectbluefin/review/pull/41",
		updatedAt: new Date(NOW).toISOString(),
		isDraft: false,
		mergeable: "MERGEABLE",
		reviewDecision: "CHANGES_REQUESTED",
		headRefOid: repairHead,
		changedFiles: 1,
		files: { pageInfo: { hasNextPage: false }, nodes: [{ path: ".github/workflows/validate.yml" }] },
		author: { login: "jorge" },
		repository: { nameWithOwner: "projectbluefin/review" },
		labels: { nodes: [] },
		commits: { nodes: [{ commit: { statusCheckRollup: { state: "FAILURE" } } }] },
	});
	const issueNode = () => ({
		number: 77,
		title: "repair existing behavior",
		url: "https://github.com/projectbluefin/review/issues/77",
		updatedAt: new Date(NOW - 1000).toISOString(),
		author: { login: "maintainer" },
		repository: { nameWithOwner: "projectbluefin/review" },
		labels: { nodes: [] },
		closedByPullRequestsReferences: { nodes: issueHasPullRequest
			? [{ number: 88, state: "OPEN", merged: false, repository: { nameWithOwner: "projectbluefin/review" } }]
			: [] },
	});
	const fetchImpl = async (url, init) => {
		if (!String(url).includes("/graphql")) {
			return { ok: true, status: 200, statusText: "OK", json: async () => [] };
		}
		const body = JSON.parse(String(init?.body ?? "{}"));
		if (body.variables?.search !== undefined) {
			const nodes = body.variables.search.includes("is:pr") ? [repairNode()] : [issueNode()];
			return {
				ok: true,
				status: 200,
				statusText: "OK",
				json: async () => ({ data: { viewer: { login: "jorge" }, search: { pageInfo: { hasNextPage: false }, nodes } } }),
			};
		}
		const node = body.query.includes("... on PullRequest") ? repairNode() : issueNode();
		return {
			ok: true,
			status: 200,
			statusText: "OK",
			json: async () => ({ data: { w0: { issueOrPullRequest: { ...node, closed: false } } } }),
		};
	};
	const pi = fakeHost();
	pi.flagValues.set("autoslay", true);
	const review = createReviewExtension(pi, { org: "projectbluefin", fetchImpl, env: ISOLATED_ENV });
	const ctx = fakeCtx();
	ctx.ui.parent = ctx;

	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(pi.messages.length, 1);
	assert.match(pi.messages[0], /Repair .*projectbluefin\/review#41/);
	assert.doesNotMatch(pi.messages[0], /projectbluefin\/review#77/);

	repairHead = "b".repeat(40);
	ctx.asyncJobs.recent = [{ id: "repair", status: "completed", startTime: Date.now() + 1 }];
	await pi.events.get("agent_end")({}, ctx);
	assert.equal(pi.messages.length, 2);
	assert.match(pi.messages[1], /Implement projectbluefin\/review#77/);
	assert.match(pi.messages[1], /hive_workbench_lookup.*queue.*knowledge/);
	assert.match(pi.messages[1], /workflowz/);
	assert.match(pi.messages[1], /Closes <owner\/repo>#<number>/);


	issueHasPullRequest = true;
	ctx.asyncJobs.recent = [{ id: "issue", status: "completed", startTime: Date.now() + 1 }];
	await pi.events.get("agent_end")({}, ctx);
	const batch = pi.entries.filter((entry) => entry.customType === BATCH_ENTRY).at(-1).data;
	assert.equal(batch.state, "complete");
	assert.equal(batch.completedItems, 2);
});
test("host Alt-S uses issue autoslay and blocks without a submitted pull request", async () => {
	const node = {
		number: 77,
		title: "repair existing behavior",
		url: "https://github.com/projectbluefin/review/issues/77",
		updatedAt: new Date(NOW).toISOString(),
		author: { login: "maintainer" },
		repository: { nameWithOwner: "projectbluefin/review" },
		labels: { nodes: [] },
		closedByPullRequestsReferences: { nodes: [] },
	};
	const fetchImpl = async (_url, init) => {
		const body = JSON.parse(String(init?.body ?? "{}"));
		const data = body.variables?.search !== undefined
			? { viewer: { login: "jorge" }, search: { pageInfo: { hasNextPage: false }, nodes: [node] } }
			: { w0: { issueOrPullRequest: { ...node, closed: false } } };
		return { ok: true, status: 200, statusText: "OK", json: async () => ({ data }) };
	};
	const pi = fakeHost();
	pi.flagValues.set("issues", true);
	const review = createReviewExtension(pi, { org: "projectbluefin", fetchImpl, env: ISOLATED_ENV });
	const ctx = fakeCtx();
	ctx.ui.parent = ctx;

	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();
	pi.shortcuts.get("alt+s").handler(ctx);
	await new Promise((resolve) => setImmediate(resolve));
	assert.match(pi.messages[0], /Implement projectbluefin\/review#77/);
	ctx.asyncJobs.recent = [{ id: "issue", status: "completed", startTime: Date.now() + 1 }];
	await pi.events.get("agent_end")({}, ctx);
	const batch = pi.entries.filter((entry) => entry.customType === BATCH_ENTRY).at(-1).data;
	assert.equal(batch.state, "blocked");
	assert.match(batch.error, /no pull request was submitted/);
});


test("ordinary PR slay blocks failed CI before reviewer dispatch", async () => {
	const item = {
		id: 42,
		repo: "projectbluefin/review",
		title: "zero-job workflow failure",
		headSha: "4".repeat(40),
		ciStatus: "failure",
		changedFiles: 1,
	};
	const pi = fakeHost();
	const review = createReviewExtension(pi, { org: "projectbluefin", fetchImpl: hiveBackedFetch([item]), env: ISOLATED_ENV });
	const ctx = fakeCtx();
	ctx.ui.parent = ctx;

	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();
	ctx.overlays[0].handleInput("s");
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(pi.messages.length, 0);
	assert.ok(ctx.notifications.some((notification) => /CI is failure/.test(notification.message)));
});

test("slay and fix fail closed on unsupported pull requests and keep them visible", async () => {
	const workflowPr = {
		number: 10,
		title: "update deploy pipeline",
		url: "https://github.com/projectbluefin/review/pull/10",
		updatedAt: new Date(NOW).toISOString(),
		isDraft: false,
		mergeable: "MERGEABLE",
		reviewDecision: "REVIEW_REQUIRED",
		headRefOid: "1".repeat(40),
		changedFiles: 1,
		files: { pageInfo: { hasNextPage: false }, nodes: [{ path: ".github/workflows/deploy.yml" }] },
		author: { login: "contributor" },
		repository: { nameWithOwner: "projectbluefin/review" },
		labels: { nodes: [] },
		commits: { nodes: [{ commit: { statusCheckRollup: { state: "SUCCESS" } } }] },
	};
	const truncatedPr = {
		number: 20,
		title: "massive refactor",
		url: "https://github.com/projectbluefin/review/pull/20",
		updatedAt: new Date(NOW - 1000).toISOString(),
		isDraft: false,
		mergeable: "MERGEABLE",
		reviewDecision: "REVIEW_REQUIRED",
		headRefOid: "2".repeat(40),
		changedFiles: 200,
		files: { pageInfo: { hasNextPage: true }, nodes: [{ path: "src/index.ts" }] },
		author: { login: "contributor" },
		repository: { nameWithOwner: "projectbluefin/review" },
		labels: { nodes: [] },
		commits: { nodes: [{ commit: { statusCheckRollup: { state: "SUCCESS" } } }] },
	};
	const eligiblePr = {
		number: 30,
		title: "fix typo",
		url: "https://github.com/projectbluefin/review/pull/30",
		updatedAt: new Date(NOW - 2000).toISOString(),
		isDraft: false,
		mergeable: "MERGEABLE",
		reviewDecision: "APPROVED",
		headRefOid: "3".repeat(40),
		changedFiles: 1,
		files: { pageInfo: { hasNextPage: false }, nodes: [{ path: "README.md" }] },
		author: { login: "contributor" },
		repository: { nameWithOwner: "projectbluefin/review" },
		labels: { nodes: [] },
		commits: { nodes: [{ commit: { statusCheckRollup: { state: "SUCCESS" } } }] },
	};
	const fetchImpl = async (_url, init) => {
		const body = JSON.parse(String(init?.body ?? "{}"));
		if (body.variables?.search !== undefined) {
			return {
				ok: true,
				status: 200,
				statusText: "OK",
				json: async () => ({
					data: {
						viewer: { login: "maintainer" },
						search: {
							pageInfo: { hasNextPage: false, endCursor: null },
							nodes: [workflowPr, truncatedPr, eligiblePr],
						},
					},
				}),
			};
		}
		const data = {};
		const aliases = /(\w+): repository\(owner: [^,]+, name: [^)]+\)\s*\{\s*issueOrPullRequest\(number: (\d+)\)/g;
		for (const [, alias, number] of body.query.matchAll(aliases)) {
			const found = [workflowPr, truncatedPr, eligiblePr].find((n) => n.number === Number(number));
			data[alias] = { issueOrPullRequest: found ? { ...found, closed: false } : null };
		}
		return { ok: true, status: 200, statusText: "OK", json: async () => ({ data }) };
	};

	const pi = fakeHost();
	const review = createReviewExtension(pi, { org: "projectbluefin", fetchImpl, env: ISOLATED_ENV });
	const ctx = fakeCtx();
	ctx.ui.parent = ctx;

	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();

	// Verify both unsupported PRs are visible in the queue tool with blocked status
	const queue = await pi.tools.get("hive_workbench_queue").execute("id", {});
	assert.match(queue.content[0].text, /\[blocked\] projectbluefin\/review#10/);
	assert.match(queue.content[0].text, /\[blocked\] projectbluefin\/review#20/);
	assert.match(queue.content[0].text, /projectbluefin\/review#30/);

	// Try fix on workflow PR -> skipped with notification, no message
	const dashboard = ctx.overlays[0];
	dashboard.handleInput("f");
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(pi.messages.length, 0);
	assert.ok(ctx.notifications.some((n) => /Skipping projectbluefin\/review#10: changes \.github\/workflows\/deploy\.yml/.test(n.message)));

	// Select truncated PR and try slay -> skipped with notification, no message
	dashboard.handleInput("j");
	dashboard.handleInput("s");
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(pi.messages.length, 0);
	assert.ok(ctx.notifications.some((n) => /Skipping projectbluefin\/review#20: complete changed-file list unavailable/.test(n.message)));

	// Select all items ("A") and slay -> unsupported PRs skipped, only eligible PR dispatched
	dashboard.handleInput("A");
	dashboard.handleInput("s");
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(pi.messages.length, 1);
	assert.match(pi.messages[0], /projectbluefin\/review#30/);
	assert.doesNotMatch(pi.messages[0], /projectbluefin\/review#10/);
	assert.doesNotMatch(pi.messages[0], /projectbluefin\/review#20/);
});

test("active slay blocks privileged and credential-bearing bash mutations", async () => {
	const pi = fakeHost();
	pi.flagValues.set("pr", "7");
	const review = createReviewExtension(pi, { org: "projectbluefin", fetchImpl: fakeFetch([]), env: ISOLATED_ENV });
	const ctx = fakeCtx();
	ctx.ui.parent = ctx;

	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();
	await new Promise((resolve) => setImmediate(resolve));
	ctx.overlays[0].handleInput("s");
	await new Promise((resolve) => setImmediate(resolve));
	const guard = pi.events.get("tool_call");
	const call = (command) => guard({ toolName: "bash", input: { command } }, ctx);

	assert.match((await call("gh pr merge 42 --repo projectbluefin/review --admin --squash")).reason, /admin merge bypass/);
	assert.match((await call("git push origin repair --force-with-lease")).reason, /force-pushing/);
	assert.match(
		(await call("git push https://x-access-token:${GH_TOKEN}@github.com/projectbluefin/review.git repair")).reason,
		/credentials in URL userinfo/,
	);
	const batch = pi.entries.filter((entry) => entry.customType === BATCH_ENTRY).at(-1).data;
	batch.waves[0].items[0].ciStatus = "failure";
	assert.match((await call("gh pr review 7 --repo projectbluefin/other --approve")).reason, /CI is failure/);
	batch.waves[0].items[0].ciStatus = "pending";
	assert.match((await call("gh pr merge 7 --repo projectbluefin/other --auto --squash")).reason, /CI is pending/);
	batch.waves[0].items[0].ciStatus = "success";
	assert.equal(await call("gh pr merge 7 --repo projectbluefin/other --auto --squash"), undefined);
});


test("slay gate refuses a blocked landing state and allows only complete readiness (review#461)", async () => {
	const pi = fakeHost();
	pi.flagValues.set("pr", "7");
	const review = createReviewExtension(pi, { org: "projectbluefin", fetchImpl: fakeFetch([]), env: ISOLATED_ENV });
	const ctx = fakeCtx();
	ctx.ui.parent = ctx;

	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();
	await new Promise((resolve) => setImmediate(resolve));
	ctx.overlays[0].handleInput("s");
	await new Promise((resolve) => setImmediate(resolve));
	const guard = pi.events.get("tool_call");
	const call = (command) => guard({ toolName: "bash", input: { command } }, ctx);

	const batch = pi.entries.filter((entry) => entry.customType === BATCH_ENTRY).at(-1).data;
	const target = batch.waves[0].items[0];

	// Green CI but a requested-changes review blocks approval and merge.
	target.reviewState = "changes_requested";
	assert.match((await call("gh pr merge 7 --repo projectbluefin/other --auto --squash")).reason, /changes_requested/);
	assert.match((await call("gh pr review 7 --repo projectbluefin/other --approve")).reason, /changes_requested/);

	// Green CI and approved but a hold label blocks.
	target.reviewState = "approved";
	target.labels = ["hold"];
	assert.match((await call("gh pr merge 7 --repo projectbluefin/other --auto --squash")).reason, /hold label/);

	// Green, approved, unheld but a dirty merge blocks.
	target.labels = [];
	target.mergeState = "dirty";
	assert.match((await call("gh pr merge 7 --repo projectbluefin/other --auto --squash")).reason, /conflicts/);

	// Green, approved, clean but pending CI blocks.
	target.mergeState = "clean";
	target.ciStatus = "pending";
	assert.match((await call("gh pr merge 7 --repo projectbluefin/other --auto --squash")).reason, /CI is pending/);

	// A green, approved, clean, unheld pull request is the only ready-to-land state.
	target.ciStatus = "success";
	assert.equal(await call("gh pr merge 7 --repo projectbluefin/other --auto --squash"), undefined);
});

test("landing state evaluates CI, reviews, holds, and mergeability together", () => {
	const ready = {
		id: 1, repo: "projectbluefin/review", ciStatus: "success", mergeState: "clean",
		reviewState: "approved", labels: [], type: "pr",
	};
	// Green, approved, clean, unheld is the only ready-to-land state.
	assert.equal(landingState(ready), "ready-to-land");
	// A requested-changes review blocks even with green CI and a clean merge.
	assert.equal(landingState({ ...ready, reviewState: "changes_requested" }), "review-blocked");
	// A hold label blocks even with green CI and an approval.
	assert.equal(landingState({ ...ready, labels: ["hold"] }), "held");
	assert.equal(landingState({ ...ready, labels: ["blocked"] }), "held");
	// A hold denied by a managed policy counts, not just the standard set.
	const policy = {
		managedRepositories: [{ repository: "projectbluefin/review", requiredLabels: [], deniedLabels: ["release-hold"] }],
	};
	assert.equal(landingState({ ...ready, labels: ["release-hold"] }, policy), "held");
	// Failing or pending CI blocks.
	assert.equal(landingState({ ...ready, ciStatus: "failure" }), "ci-failing");
	assert.equal(landingState({ ...ready, ciStatus: "pending" }), "ci-pending");
	// A dirty merge blocks.
	assert.equal(landingState({ ...ready, mergeState: "dirty" }), "conflicts");
	// Awaiting approval is not ready.
	assert.equal(landingState({ ...ready, reviewState: "review_required" }), "unreviewed");
	// An issue is not a landing target.
	assert.equal(landingState({ ...ready, type: "issue" }), "incomplete");
	// A hold is checked before CI, so a hold never reads as merely pending CI.
	assert.equal(landingState({ ...ready, ciStatus: "failure", labels: ["hold"] }), "held");
	// isLandingReady is true only for the complete state.
	assert.equal(isLandingReady(ready), true);
	assert.equal(isLandingReady({ ...ready, reviewState: "changes_requested" }), false);
	assert.equal(isLandingReady({ ...ready, labels: ["hold"] }), false);
	// The standard hold set is exported for callers that need it.
	assert.deepEqual([...HOLD_LABELS].sort(), ["blocked", "hold"]);
});

test("landing reason names the blocker for gates and the UI", () => {
	assert.equal(landingReason("ci-failing"), "CI is failure");
	assert.equal(landingReason("ci-pending"), "CI is pending");
	assert.equal(landingReason("review-blocked"), "has a changes_requested review");
	assert.equal(landingReason("held"), "has a hold label");
	assert.equal(landingReason("conflicts"), "has conflicts with the base");
	assert.equal(landingReason("unreviewed"), "is awaiting approval");
	assert.equal(landingReason("incomplete"), "is not a pull request");
	assert.equal(landingReason("ready-to-land"), "");
});


test("RAW_KEYS normalizes Alt-S and Alt-B chords", () => {
	assert.equal(canonicalKey("\u001bs", rawKeyMatcher), "alt+s");
	assert.equal(canonicalKey("\u001bb", rawKeyMatcher), "alt+b");
	assert.equal(canonicalKey("\u001bu", rawKeyMatcher), "alt+u");
});

test("comment action previews once, revalidates live state, executes argv, and persists a receipt", async () => {
	const items = [{ id: 42, repo: "projectbluefin/review", title: "comment target", headSha: "a".repeat(40) }];
	const pi = fakeHost();
	const ctx = fakeCtx();
	ctx.ui.parent = ctx;
	ctx.editorResponses.push("Evidence-backed comment");
	const review = createReviewExtension(pi, {
		org: "projectbluefin",
		fetchImpl: hiveBackedFetch(items),
		env: { ...ISOLATED_ENV, HIVE_HUB: "wss://hive.example/contribute" },
	});
	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();
	const dashboard = ctx.overlays[0];
	dashboard.handleInput("c");
	await new Promise((resolve) => setImmediate(resolve));

	assert.equal(ctx.overlays.length, 1, "comment action keeps the workbench mounted");
	assert.equal(ctx.confirmations.length, 1, "one immutable plan is confirmed once");
	assert.match(ctx.confirmations[0].message, /projectbluefin\/review#42/);
	assert.deepEqual(pi.execCalls, [{
		command: "gh",
		args: ["pr", "comment", "42", "--repo", "projectbluefin/review", "--body", "Evidence-backed comment"],
	}]);
	const receipt = pi.entries.filter((entry) => entry.customType === COMMENT_ENTRY).at(-1).data;
	assert.equal(receipt.state, "complete");
	assert.deepEqual(receipt.receipts, [pi.execResult.stdout.trim()]);
});

test("comment batches revalidate later heads and retain partial receipts", async () => {
	const items = [
		{ id: 42, repo: "projectbluefin/review", title: "first target", headSha: "a".repeat(40) },
		{ id: 43, repo: "projectbluefin/review", title: "later target", headSha: "b".repeat(40) },
	];
	const pi = fakeHost();
	const execute = pi.exec.bind(pi);
	pi.exec = async (command, args) => {
		const result = await execute(command, args);
		items[1].headSha = "c".repeat(40);
		return result;
	};
	const ctx = fakeCtx();
	ctx.editorResponses.push("Review evidence");
	const review = createReviewExtension(pi, {
		org: "projectbluefin",
		fetchImpl: (url, init) => hiveBackedFetch(items)(url, init),
		env: { ...ISOLATED_ENV, HIVE_HUB: "wss://hive.example/contribute" },
	});
	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();
	ctx.overlays[0].handleInput("A");
	ctx.overlays[0].handleInput("c");
	const { promise, resolve } = Promise.withResolvers<void>();
	setImmediate(resolve);
	await promise;

	assert.equal(pi.execCalls.length, 1, "a changed second head must not receive the stale comment");
	assert.equal(pi.execCalls[0].args[2], "42");
	const receipt = pi.entries.filter((entry) => entry.customType === COMMENT_ENTRY).at(-1).data;
	assert.equal(receipt.state, "failed");
	assert.match(receipt.error, /PR head changed/);
	assert.deepEqual(receipt.receipts, [pi.execResult.stdout.trim()]);
});

test("pinned OMP agent_end advances repository waves only after final settlement", async () => {
	const items = [
		{ id: 1, repo: "projectbluefin/a", title: "a one", headSha: "1".repeat(40), autoMergeEnabled: true },
		{ id: 2, repo: "projectbluefin/a", title: "a two", headSha: "2".repeat(40), autoMergeEnabled: true },
		{ id: 3, repo: "projectbluefin/b", title: "b one", headSha: "3".repeat(40), autoMergeEnabled: true },
	];
	const pi = fakeHost();
	const ctx = fakeCtx();
	ctx.ui.parent = ctx;
	const review = createReviewExtension(pi, {
		org: "projectbluefin",
		fetchImpl: hiveBackedFetch(items),
		env: { ...ISOLATED_ENV, HIVE_HUB: "wss://hive.example/contribute" },
	});
	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();
	const dashboard = ctx.overlays[0];
	dashboard.handleInput("A");
	dashboard.handleInput("s");
	await new Promise((resolve) => setImmediate(resolve));

	assert.equal(pi.messages.length, 1);
	assert.match(pi.messages[0], /^Slay this repository wave for projectbluefin\/a through review, repair, and landing:/m);
	assert.match(pi.messages[0], /Use the `task` tool once with one fresh bluefin-reviewer item per pull request/);
	assert.match(pi.messages[0], /projectbluefin\/a/);
	assert.doesNotMatch(pi.messages[0], /projectbluefin\/b/);

	ctx.asyncJobs.recent = [{ id: "hive-wave", status: "completed", startTime: Date.now() + 1 }];
	await pi.events.get("agent_end")({ willContinue: true }, ctx);
	assert.equal(pi.messages.length, 1, "an OMP continuation must not admit another repository");
	ctx.asyncJobs.running = [{ id: "still-running", status: "running", startTime: Date.now() + 1 }];
	await pi.events.get("agent_end")({}, ctx);
	assert.equal(pi.messages.length, 1, "unfinished workers must settle before another repository starts");
	ctx.asyncJobs.running = [];

	dashboard.handleInput("p");
	ctx.asyncJobs.recent = [{ id: "hive-wave", status: "completed", startTime: Date.now() + 1 }];
	await pi.events.get("agent_end")({}, ctx);
	assert.equal(pi.messages.length, 1, "pause prevents the next repository from starting");
	dashboard.handleInput("p");
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(pi.messages.length, 2);
	assert.match(pi.messages[1], /projectbluefin\/b/);
	assert.doesNotMatch(pi.messages[1], /projectbluefin\/a/);

	ctx.asyncJobs.recent = [{ id: "hive-wave-2", status: "completed", startTime: Date.now() + 1 }];
	await pi.events.get("agent_end")({}, ctx);
	const batches = pi.entries.filter((entry) => entry.customType === BATCH_ENTRY).map((entry) => entry.data);
	assert.equal(batches.at(-1).state, "complete");
	assert.equal(batches.at(-1).completedItems, 3);
	assert.ok(dashboard.render(120).some((line) => line.includes("3/3 terminal")));
});

test("a slay wave blocks when review jobs leave pull requests open", async () => {
	const item = { id: 42, repo: "projectbluefin/review", title: "reviewed but not landed", headSha: "a".repeat(40) };
	const pi = fakeHost();
	const ctx = fakeCtx();
	ctx.ui.parent = ctx;
	const review = createReviewExtension(pi, {
		org: "projectbluefin",
		fetchImpl: hiveBackedFetch([item]),
		env: { ...ISOLATED_ENV, HIVE_HUB: "wss://hive.example/contribute" },
	});
	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();
	ctx.overlays[0].handleInput("s");
	await new Promise((resolve) => setImmediate(resolve));
	ctx.asyncJobs.recent = [{ id: "review-only", status: "completed", startTime: Date.now() + 1 }];
	await pi.events.get("agent_end")({}, ctx);

	const batch = pi.entries.filter((entry) => entry.customType === BATCH_ENTRY).at(-1).data;
	assert.equal(batch.state, "blocked");
	assert.match(batch.error, /projectbluefin\/review#42/);
	assert.ok(ctx.notifications.some((notification) => /remain open without auto-merge/.test(notification.message)));
});



test("a failed workflowz job blocks later repository waves", async () => {
	const items = [
		{ id: 1, repo: "projectbluefin/a", title: "a one" },
		{ id: 2, repo: "projectbluefin/a", title: "a two" },
		{ id: 3, repo: "projectbluefin/b", title: "b one" },
	];
	const pi = fakeHost();
	const ctx = fakeCtx();
	ctx.ui.parent = ctx;
	const review = createReviewExtension(pi, {
		org: "projectbluefin",
		fetchImpl: hiveBackedFetch(items),
		env: { ...ISOLATED_ENV, HIVE_HUB: "wss://hive.example/contribute" },
	});
	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();
	const dashboard = ctx.overlays[0];
	dashboard.handleInput("A");
	dashboard.handleInput("s");
	await new Promise((resolve) => setImmediate(resolve));
	ctx.asyncJobs.recent = [{ id: "failed-worker", status: "failed", startTime: Date.now() + 1 }];
	await pi.events.get("agent_end")({}, ctx);

	assert.equal(pi.messages.length, 1, "a failed wave never advances to the next repository");
	const slay = pi.entries.filter((entry) => entry.customType === BATCH_ENTRY).at(-1).data;
	assert.equal(slay.state, "blocked");
	assert.match(slay.error, /workflowz job.*failed/);
});
test("restart blocks interrupted slays and never replays confirmed comments", async () => {
	const item = { id: 42, repo: "projectbluefin/review", title: "recover safely", headSha: "a".repeat(40) };
	const commentPlan = createCommentActionPlan([
		{ repo: item.repo, number: item.id, type: "pull_request", headSha: item.headSha },
	], "Do not replay me", 100);
	const pi = fakeHost();
	const ctx = fakeCtx();
	ctx.ui.parent = ctx;
	ctx.sessionManager = {
		getBranch: () => [
			{ type: "custom", customType: STATE_ENTRY, data: { mode: "prs" } },
			{
				type: "custom",
				customType: BATCH_ENTRY,
				data: {
					version: 1,
					id: "slay-old",
					kind: "slay",
					waves: [{ repo: item.repo, items: [{ ...item, type: "pr" }] }],
					currentWave: 0,
					completedItems: 0,
					totalItems: 1,
					state: "running",
					startedAt: 50,
					waveStartedAt: 60,
				},
			},
			{ type: "custom", customType: COMMENT_ENTRY, data: { version: 1, state: "confirmed", plan: commentPlan } },
		],
	};
	const review = createReviewExtension(pi, {
		org: "projectbluefin",
		fetchImpl: hiveBackedFetch([item]),
		env: { ...ISOLATED_ENV, HIVE_HUB: "wss://hive.example/contribute" },
	});
	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();

	assert.equal(pi.execCalls.length, 0, "confirmed comments are never replayed after restart");
	assert.equal(pi.messages.length, 0, "interrupted repository waves are never replayed");
	assert.ok(ctx.notifications.some((notification) => /interrupted comment plan was not replayed/.test(notification.message)));
	const recovered = pi.entries.filter((entry) => entry.customType === BATCH_ENTRY).at(-1).data;
	assert.equal(recovered.state, "blocked");
	assert.match(recovered.error, /session ended/);
	assert.ok(ctx.overlays[0].render(240).some((line) => line.includes("BLOCKED")));
});

test("slay prompts define bounded review, isolated repair, and live-rule landing", () => {
	const item = queueItem();
	const sibling = queueItem({ id: 7, repo: item.repo });
	const slay = actionPrompt({ kind: "slay", item, items: [item, sibling] });
	const fix = actionPrompt({ kind: "fix", item, items: [item, sibling] });
	for (const prompt of [slay, fix]) {
		assert.match(prompt, /Never sleep or poll/);
		assert.match(prompt, /Evidence is bounded and read once/);
		assert.match(prompt, /--name-only/);
		assert.doesNotMatch(prompt, /--json [\w,]*\bbody\b/);
	}
	assert.match(slay, /`task` tool once with one fresh bluefin-reviewer item per pull request/);
	assert.match(slay, /Do not use eval workpool/);
	assert.match(slay, /fresh isolated fixer/);
	assert.match(slay, /both `pull_request` and explicit `repo`/);
	assert.match(slay, /\$HOME\/worktrees/);
	assert.match(slay, /rules\/branches\/<branch>/);
	assert.match(slay, /reviewed head must equal the live head/i);
	assert.match(slay, /gh pr merge <n> --repo <r> --auto --squash/);
	assert.match(slay, /Never use `--admin`/);
	assert.match(slay, /do not disable and re-arm auto-merge/);
	assert.match(slay, /report the outstanding approval gate and move on/);
	const reviewerPrompt = readFileSync("image/extension/bluefin-review/agents/bluefin-reviewer.md", "utf8");
	assert.match(reviewerPrompt, /hive_workbench_diff\(pull_request: <number>, repo: "<owner\/name>"\)/);
	assert.match(reviewerPrompt, /Do not assume a local checkout exists/);
	const reviewerTools = reviewerPrompt.match(/^tools: (.+)$/m)?.[1] ?? "";
	assert.doesNotMatch(reviewerTools, /\b(?:bash|yield)\b/);
	assert.match(reviewerPrompt, /strictly read-only/);
	assert.match(reviewerPrompt, /A `clean` verdict is\s+evidence/);
	assert.doesNotMatch(reviewerPrompt, /\*\*`approve`\*\*/);
	assert.match(reviewerPrompt, /Never claim a validator is absent/);
	assert.match(fix, /`task` tool once with one fresh isolated item per issue or pull request/);
	assert.match(fix, /Never approve or merge/);
});

test("fix waves repair conflicts without landing them", () => {
	const dirty = queueItem({ ciStatus: "failure", mergeState: "dirty", reviewState: "review_required" });
	const sibling = queueItem({ id: 7, repo: dirty.repo, mergeState: "dirty" });
	const batch = actionPrompt({ kind: "fix", item: dirty, items: [dirty, sibling] });
	assert.match(batch, /merge=dirty/);
	assert.match(batch, /`task` tool once with one fresh isolated item/);
	assert.match(batch, /Never approve or merge/);
});

test("the queue read travels with its caveat and workflowz rules are copyable", () => {
	const green = queueItem({ ciStatus: "success", mergeState: "clean", reviewState: "approved" });
	const sibling = queueItem({ id: 7, repo: green.repo, ciStatus: "failure" });
	const batch = actionPrompt({ kind: "fix", item: green, items: [green, sibling] });
	for (const line of batch.split("\n").filter((line) => line.includes("queue read:"))) {
		assert.match(line, /revalidate live before mutating/, line);
	}
	assert.match(batch, /<<<SUBAGENT-RULES[\s\S]*Never sleep or poll[\s\S]*SUBAGENT-RULES>>>/);
	assert.match(batch, /Copy this block verbatim/);
});


// The timeout is the assertion: startup registers its refreshers and returns
// before a slow GitHub read can consume OMP's extension-handler budget.
test("session_start returns while the queue is still loading", { timeout: 5000 }, async () => {
	const pi = fakeHost();
	const stalled = Promise.withResolvers();
	const review = createReviewExtension(pi, {
		org: "projectbluefin",
		fetchImpl: () => stalled.promise,
		env: ISOLATED_ENV,
	});
	const ctx = fakeCtx();
	ctx.ui.parent = ctx;

	await pi.events.get("session_start")({}, ctx);
	assert.equal(ctx.overlays.length, 0, "no second surface is mounted while data loads");
	assert.equal(typeof ctx.widgets.get("hive-workbench-rail"), "function", "the rail is mounted before the first await");

	stalled.resolve({ ok: false, status: 504, statusText: "Gateway Timeout", json: async () => ({}) });
	await review.whenStarted();
	assert.equal(ctx.overlays.length, 1, "the one workbench opens after the initial queue read");
	assert.ok(
		ctx.notifications.some((entry) => entry.level === "error" && /504/.test(entry.message)),
		"a queue that failed says so instead of rendering as empty",
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
	;

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
	const queue = await pi.tools.get("hive_workbench_queue").execute("id", {});
	assert.match(queue.content[0].text, /projectbluefin\/other#7/);
	assert.equal(queue.details.items.length, 2);
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
	;
	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();
	assert.ok(ctx.statuses.get("hive_workbench")?.includes("#7"));
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
		;
		await pi.events.get("session_start")({}, ctx);
		await review.whenStarted();
		return await pi.tools.get("hive_workbench_status").execute("id", {});
	};

	const hubEnv = { ...ISOLATED_ENV, HIVE_HUB: "https://hive.example" };

	const noHub = await statusFor(fakeFetch([]), ISOLATED_ENV);
	assert.match(noHub.content[0].text, /order: unranked — no hive hub configured/);
	assert.equal(noHub.details.hive.configured, false);

	const quiet = await statusFor(hiveFetch([]), hubEnv);
	assert.match(quiet.content[0].text, /order: hive — 0 of 2 items ranked/);
	assert.equal(quiet.details.hive.online, true);
	assert.equal(quiet.details.order_source, "hive", "an online hub remains the ordering authority even when nothing is ranked in scope");

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
	// The header/status line is a concise fallback status, not the raw error.
	assert.match(unreachable.content[0].text, /order: unavailable — hive unavailable; queue order falls back to GitHub, and review, fix, and slay remain available/);
	// The raw diagnostic is kept behind the status, in the structured details.
	assert.match(unreachable.details.hive.error ?? "", /502/, "the raw error stays in the status details, not the header");
});

// ---------------------------------------------------------------- hive fallback

test("an optional Hive failure degrades to a concise fallback status", () => {
	// Classified by signature, so a TLS cert mismatch, a timeout, a dropped
	// connection and an auth failure each read as a distinct short label rather
	// than a raw error string, and anything unknown falls back cleanly.
	const tls = new Error("alert certificate name invalid");
	(tls as { code?: string }).code = "ERR_TLS_CERT_ALTNAME_INVALID";
	assert.equal(hiveFailureStatus(tls), "hive tls", "a TLS mismatch is a tls status");

	const timeout = new Error("read failed");
	(timeout as { code?: string }).code = "ETIMEDOUT";
	assert.equal(hiveFailureStatus(timeout), "hive network");

	const refused = new Error("connect failed");
	(refused as { code?: string }).code = "ECONNREFUSED";
	assert.equal(hiveFailureStatus(refused), "hive connection");

	const denied = new Error("403 Forbidden");
	assert.equal(hiveFailureStatus(denied), "hive unauthorized");

	const dns = new Error("getaddrinfo ENOTFOUND hub");
	assert.equal(hiveFailureStatus(dns), "hive network");

	assert.equal(hiveFailureStatus("/api/contribute/status → 502 Bad Gateway"), "hive unavailable");
});

test("the rail header shows the concise status and never the raw error", () => {
	const mode = new ReviewMode({ org: "projectbluefin" });
	mode.hive = {
		...EMPTY_HIVE,
		hub: "https://hive.example",
		configured: true,
		online: false,
		error: "getaddrinfo ENOTFOUND hive.example",
		fetchedAt: NOW,
	};
	mode.items = [queueItem()];
	mode.reprioritize();

	const rows = renderRail(mode, PLAIN_PAINTER, 120, NOW, 0, []);
	assert.match(rows[0], /hive network/, "a network failure reads as a concise status");
	assert.doesNotMatch(rows[0], /ENOTFOUND/, "the raw diagnostic must not dominate the header");
	assert.doesNotMatch(rows[0], /hive\.example/, "the hub host is not a status line");
	assert.equal(mode.orderSource(), "local", "a broken hub falls back to local order");
});

test("the status tool names all three optional-Hive states distinctly", async () => {
	const hubEnv = { ...ISOLATED_ENV, HIVE_HUB: "https://hive.example" };
	const statusFor = async (fetchImpl, env) => {
		const pi = fakeHost();
		const review = createReviewExtension(pi, { org: "projectbluefin", fetchImpl, env });
		const ctx = fakeCtx();
		ctx.ui.parent = ctx;
		;
		await pi.events.get("session_start")({}, ctx);
		await review.whenStarted();
		return await pi.tools.get("hive_workbench_status").execute("id", {});
	};

	// 1. Unconfigured: no hub at all.
	const noHub = await statusFor(fakeFetch([]), ISOLATED_ENV);
	assert.match(noHub.content[0].text, /order: unranked — no hive hub configured/);
	assert.equal(noHub.details.hive.configured, false);

	// 2. Online with nothing queued in this scope: not an error, just empty.
	const quiet = await statusFor(
		async (url, init) => {
			if (String(url).includes("/graphql")) return fakeFetch([])(url, init);
			const path = String(url).replace("https://hive.example", "");
			return { ok: true, status: 200, statusText: "OK", json: async () => (path === "/api/v1/status" ? { actionable_items: 3 } : { queue: [], groups: [] }) };
		},
		hubEnv,
	);
	assert.match(quiet.content[0].text, /order: hive — 0 of 2 items ranked/);
	assert.equal(quiet.details.hive.online, true);

	// 3. Configured but broken: a concise status up front, the raw error behind it.
	const broken = await statusFor(
		async (url, init) => {
			if (String(url).includes("/graphql")) return fakeFetch([])(url, init);
			return { ok: false, status: 502, statusText: "Bad Gateway", json: async () => ({}) };
		},
		hubEnv,
	);
	assert.match(broken.content[0].text, /order: unavailable — hive unavailable; queue order falls back to GitHub, and review, fix, and slay remain available/);
	assert.match(broken.details.hive.error ?? "", /502/, "the raw error is the diagnostic, kept in the status details");
	assert.equal(broken.details.hive.online, false);
});

test("a hive-only session with a broken hub still fails visibly and concisely", async () => {
	const hubEnv = { ...ISOLATED_ENV, HIVE_HUB: "https://hive.example" };
	const hiveFetch = async (url, init) => {
		if (String(url).includes("/graphql")) return fakeFetch([])(url, init);
		return { ok: false, status: 502, statusText: "Bad Gateway", json: async () => ({}) };
	};
	const pi = fakeHost();
	const review = createReviewExtension(pi, { org: "projectbluefin", fetchImpl: hiveFetch, env: hubEnv });
	const ctx = fakeCtx();
	ctx.ui.parent = ctx;
	;
	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();

	assert.ok(
		ctx.notifications.some((notification) => /hive unavailable; queue order falls back to GitHub/.test(notification.message)),
		`a broken hive must fail visibly at startup, got ${JSON.stringify(ctx.notifications)}`,
	);
	const startupHive = ctx.notifications.find((notification) => /queue order falls back to GitHub/.test(notification.message));
	assert.doesNotMatch(startupHive.message, /502/);
});


test("action prompts reserve landing authority for slay", () => {
	const item = queueItem();
	assert.match(actionPrompt({ kind: "slay", item }), /hive_workbench_diff/);
	assert.match(actionPrompt({ kind: "slay", item }), /hive_workbench_trace/);
	assert.match(actionPrompt({ kind: "slay", item }), /bluefin-reviewer/);
	assert.match(actionPrompt({ kind: "fix", item }), /Never approve or merge/);
	const slayAction = { kind: "slay", item, items: [item, queueItem({ id: 7, repo: item.repo })] };
	const slayPrompt = actionPrompt(slayAction);
	assert.match(slayPrompt, /Use the `task` tool once with one fresh bluefin-reviewer item per pull request/);
	assert.match(slayPrompt, /review, repair, and landing/);
	assert.match(slayPrompt, /Report one terminal outcome per item/);
	assert.doesNotMatch(slayPrompt, /requires? (?:two|2) approvals?/i);
	const issue = queueItem({ id: 8, type: "issue", reviewState: "unknown" });
	assert.equal(actionPrompt({ kind: "slay", item, items: [item, issue] }), undefined);
	assert.equal(actionPrompt({ kind: "close" }), undefined);
});

test("diff prompts match the object: issues inspect discussion, pull requests diff", () => {
	// Issue #591: `d` on an issue must not send the agent to the PR-only
	// diff tool. Issue inspection reads the body, discussion, and linked PRs.
	const pr = queueItem();
	const issue = queueItem({ id: 8, type: "issue", reviewState: "unknown" });

	const prPrompt = actionPrompt({ kind: "diff", item: pr });
	assert.match(prPrompt, /hive_workbench_diff/);

	const issuePrompt = actionPrompt({ kind: "diff", item: issue });
	assert.match(issuePrompt, /gh issue view 8 --repo projectbluefin\/review --comments/);
	assert.match(issuePrompt, /linked/);
	assert.doesNotMatch(issuePrompt, /Call hive_workbench_diff/);

	const issueWave = actionPrompt({ kind: "diff", item: issue, items: [issue, queueItem({ id: 9, type: "issue", reviewState: "unknown" })] });
	assert.match(issueWave, /Inspect this issue wave/);
	assert.match(issueWave, /body, discussion, and linked pull requests/);
	assert.doesNotMatch(issueWave, /Use hive_workbench_diff/);
	const prWave = actionPrompt({ kind: "diff", item: pr, items: [pr, queueItem({ id: 7, repo: pr.repo })] });
	assert.match(prWave, /hive_workbench_diff/);
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
	;
	pi.flagValues.set("issues", true);
	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();

	const queue = await pi.tools.get("hive_workbench_queue").execute("id", {});
	assert.match(queue.content[0].text, /projectbluefin\/lab#470/, "hive work outside the search must still be queued");
	assert.equal(queue.details.order_source, "hive");

	// #936 resolved to nothing — moved or unreadable — and #466 came back closed.
	// Neither is in the queue, and the shortfall is reported rather than hidden
	// behind a list that merely looks complete.
	assert.doesNotMatch(queue.content[0].text, /#936/);
	assert.doesNotMatch(queue.content[0].text, /#466/, "finished work is not a queue");
	const status = await pi.tools.get("hive_workbench_status").execute("id", {});
	assert.equal(status.details.hive.queued.present, 1);
	assert.equal(status.details.hive.queued.total, 3);
});

test("the dashboard drills into Hive's queue by stage and explains each item", (t) => {
	const root = mkdtempSync(join(tmpdir(), "workbench-test-"));
	t.after(() => rmSync(root, { recursive: true, force: true }));
	const mode = new ReviewMode({ org: "projectbluefin" });

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
	;
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
	const root = mkdtempSync(join(tmpdir(), "workbench-test-"));
	t.after(() => rmSync(root, { recursive: true, force: true }));
	const mode = new ReviewMode({ org: "projectbluefin" });
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

	const batch = mode.chosenItems();
	const prompt = actionPrompt({ kind: "fix", item: batch[0], items: batch });
	assert.match(prompt, /Use the `task` tool once/);
	assert.match(prompt, /`task` tool once with one fresh isolated item per issue through OMP workflowz/);
	assert.doesNotMatch(prompt, /maximum of 7|fix-and-merge|approve and merge/);
});


test("issue admission gate handles positive admission, negative cases, and invariants", async () => {
	const setup = async (issueAttrs, options = {}) => {
		const pi = fakeHost();
		const issues = Array.isArray(issueAttrs) ? issueAttrs : [issueAttrs];
		const issueFetch = (url, init) => {
			const target = String(url);
			if (target.includes("/graphql")) {
				const body = JSON.parse(String(init?.body ?? "{}"));
				// Distinguish search query vs admission query
				if (body.variables?.search !== undefined) {
					return {
						ok: true,
						status: 200,
						statusText: "OK",
						json: async () => ({
							data: {
								search: {
									pageInfo: { hasNextPage: false, endCursor: null },
								nodes: issues.map((it) => ({
									number: it.number ?? 485,
									title: it.title ?? "test issue",
									url: `https://github.com/${it.repo ?? "projectbluefin/review"}/${it.type === "pr" ? "pull" : "issues"}/${it.number ?? 485}`,
									updatedAt: new Date(NOW - 1000).toISOString(),
									isDraft: false,
									mergeable: "MERGEABLE",
									reviewDecision: "REVIEW_REQUIRED",
									author: { login: "someone" },
									repository: { nameWithOwner: it.repo ?? "projectbluefin/review" },
									labels: { nodes: (it.labels ?? []).map((label) => ({ name: label })) },
									headRefOid: it.headSha ?? "a".repeat(40),
								})),
								},
							},
						}),
					};
				}
				if (body.query.includes("issueOrPullRequest")) {
					const data = {};
					const exactRegex = /(\w+): repository\(owner: "([^"]+)", name: "([^"]+)"\)[\s\S]*?issueOrPullRequest\(number: (\d+)\)/g;
					for (const [, alias, owner, repo, numberStr] of body.query.matchAll(exactRegex)) {
						const num = Number(numberStr);
						const fullRepo = `${owner}/${repo}`;
						const it = issues.find((candidate) => (candidate.number ?? 485) === num && (candidate.repo ?? "projectbluefin/review") === fullRepo);
						data[alias] = it ? { issueOrPullRequest: {
							closed: false,
							number: num,
							title: it.title ?? "test pull request",
							url: `https://github.com/${fullRepo}/pull/${num}`,
							updatedAt: new Date(NOW - 1000).toISOString(),
							author: { login: "someone" },
							repository: { nameWithOwner: fullRepo },
							labels: { nodes: (it.labels ?? []).map((label) => ({ name: label })) },
							isDraft: false,
							mergeable: "MERGEABLE",
							reviewDecision: "REVIEW_REQUIRED",
							changedFiles: 1,
							headRefOid: it.headSha ?? "a".repeat(40),
							commits: { nodes: [{ commit: { statusCheckRollup: { state: "SUCCESS" } } }] },
							closingIssuesReferences: { nodes: [] },
						} } : { issueOrPullRequest: null };
					}
					return { ok: true, status: 200, statusText: "OK", json: async () => ({ data }) };
				}

				// If options.fetchError is provided, simulate request failure on admission
				if (options.networkError) {
					throw new Error("network connection reset");
				}
				if (options.httpStatus) {
					return { ok: false, status: options.httpStatus, statusText: "Error", json: async () => ({}) };
				}
				if (options.graphqlErrors) {
					return { ok: true, status: 200, statusText: "OK", json: async () => ({ errors: options.graphqlErrors }) };
				}


				// Admission query by alias: parse targets from the query
				const data = {};
				const aliasRegex = /(\w+): repository\(owner: "([^"]+)", name: "([^"]+)"\)\s*\{\s*nameWithOwner\s*issue\(number: (\d+)\)/g;
				for (const [, alias, owner, repo, numberStr] of body.query.matchAll(aliasRegex)) {
					const num = Number(numberStr);
					const fullRepo = `${owner}/${repo}`;
					const it = issues.find((i) => (i.number ?? 485) === num && (i.repo ?? "projectbluefin/review") === fullRepo) ?? {
						number: num,
						repo: fullRepo,
						labels: [],
					};
					if (options.missingNode) {
						data[alias] = { nameWithOwner: fullRepo, issue: null };
						continue;
					}
					const returnedRepo = options.wrongRepo ? "projectbluefin/wrong" : fullRepo;
					const returnedNumber = options.wrongNumber ? 999 : num;
					data[alias] = {
						nameWithOwner: returnedRepo,
						issue: {
							number: returnedNumber,
							closed: options.missingClosed ? undefined : it.closed === true,
							repository: { nameWithOwner: returnedRepo },
							labels: options.missingLabelPageInfo
								? { nodes: (it.admissionLabels ?? it.labels ?? []).map((l) => ({ name: l })) }
								: {
										pageInfo: { hasNextPage: options.labelsTruncated === true },
										nodes: (it.admissionLabels ?? it.labels ?? []).map((l) => ({ name: l })),
									},
						},
					};
				}
				return { ok: true, status: 200, statusText: "OK", json: async () => ({ data }) };
			}
			return { ok: true, status: 200, statusText: "OK", json: async () => [] };
		};
		const fetchImpl = (url, init) => {
			const target = String(url);
			if (target.endsWith("/api/v1/status")) {
				return Promise.resolve({ ok: true, status: 200, statusText: "OK", json: async () => ({ actionable_items: issues.length }) });
			}
			if (target.endsWith("/api/contribute/queue")) {
				return Promise.resolve({
					ok: true,
					status: 200,
					statusText: "OK",
					json: async () => ({ queue: issues.map((item) => ({
						key: `${item.repo ?? "projectbluefin/review"}#${item.number ?? 485}`,
						repo: item.repo ?? "projectbluefin/review",
						number: item.number ?? 485,
						title: item.title ?? "test issue",
					})) }),
				});
			}
			if (target.endsWith("/api/contribute/triage")) return Promise.resolve({ ok: true, status: 200, statusText: "OK", json: async () => ({ groups: [] }) });
			if (target.endsWith("/api/v1/contributors")) return Promise.resolve({ ok: true, status: 200, statusText: "OK", json: async () => ({ contributors: [] }) });
			return (options.customFetch ?? issueFetch)(url, init);
		};

		const review = createReviewExtension(pi, {
			org: "projectbluefin",
			fetchImpl,
			env: { ...ISOLATED_ENV, HIVE_HUB: "wss://hive.example/contribute" },
			policy: BLUEFIN_POLICY,
		});
		const ctx = fakeCtx();
		ctx.ui.parent = ctx;
		;
		if (!options.isPr) pi.flagValues.set("issues", true);
		await pi.events.get("session_start")({}, ctx);
		await review.whenStarted();
		const dashboard = ctx.overlays[0];
		const turn = async () => {
			for (let i = 0; i < 20; i++) await Promise.resolve();
		};
		return { pi, ctx, dashboard, mode: review, turn };
	};

	// 1. Positive exact-label admission with exactly one dispatch
	{
		const { pi, dashboard, turn } = await setup({ number: 485, labels: ["3-clanker-queue"] });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();

		assert.equal(pi.messages.length, 1, "admitted issue dispatches exactly once");
		assert.match(pi.messages[0], /Implement projectbluefin\/review#485/);
	}
	// Issue slay uses the same admission gate before implementation.
	{
		const { pi, dashboard, turn } = await setup({ number: 486, labels: ["3-clanker-queue"] });
		pi.messages.length = 0;
		dashboard.handleInput("s");
		await turn();
		assert.equal(pi.messages.length, 1, "admitted issue slay dispatches exactly once");
		assert.match(pi.messages[0], /hive_workbench_lookup.*queue.*knowledge/);
	}
	{
		const { pi, dashboard, ctx, turn } = await setup({ number: 487, labels: [] });
		pi.messages.length = 0;
		dashboard.handleInput("s");
		await turn();
		assert.equal(pi.messages.length, 0, "unadmitted issue slay does not dispatch");
		assert.ok(ctx.notifications.some((n) => n.message.includes("missing explicit admission label")));
	}

	// 2. No label
	{
		const { pi, dashboard, ctx, turn } = await setup({ number: 485, labels: [] });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "no label -> no dispatch");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("missing explicit admission label")));
	}

	// 3. Only 3-human-queue
	{
		const { pi, dashboard, ctx, turn } = await setup({ number: 485, labels: ["3-human-queue"] });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "3-human-queue only -> no dispatch");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("missing explicit admission label")));
	}

	// 4. Only hive/* or agent/* provenance
	{
		const { pi, dashboard, ctx, turn } = await setup({ number: 485, labels: ["hive/triage", "agent/task", "area/image"] });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "hive/agent labels only -> no dispatch");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("missing explicit admission label")));
	}

	// 5. hold label present (even if 3-clanker-queue is present)
	{
		const { pi, dashboard, ctx, turn } = await setup({ number: 485, labels: ["3-clanker-queue", "hold"] });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "hold label -> no dispatch");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("hold label")));
	}

	// 6. blocked label present (even if 3-clanker-queue is present)
	{
		const { pi, dashboard, ctx, turn } = await setup({ number: 485, labels: ["3-clanker-queue", "blocked"] });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "blocked label -> no dispatch");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("blocked label")));
	}

	// 7. closed issue
	{
		const { pi, dashboard, ctx, turn } = await setup({ number: 485, labels: ["3-clanker-queue"], closed: true });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "closed issue -> no dispatch");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("issue is closed")));
	}

	// 8. Admission removed between display and dispatch
	{
		// Queue showed 3-clanker-queue, but admission check returns labels: []
		const { pi, dashboard, ctx, turn } = await setup({ number: 485, labels: ["3-clanker-queue"], admissionLabels: [] });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "label removed between display and dispatch -> no dispatch");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("missing explicit admission label")));
	}

	// 9. Request failure (network error / HTTP error / GraphQL partial errors)
	{
		const { pi, dashboard, ctx, turn } = await setup({ number: 485, labels: ["3-clanker-queue"] }, { networkError: true });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "network error -> no dispatch");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("Admission check failed")));
	}
	{
		const { pi, dashboard, ctx, turn } = await setup({ number: 485, labels: ["3-clanker-queue"] }, { graphqlErrors: [{ message: "rate limited" }] });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "graphql error -> no dispatch");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("rate limited")));
	}

	// 10. Incomplete label evidence (labelsTruncated)
	{
		const { pi, dashboard, ctx, turn } = await setup({ number: 485, labels: ["3-clanker-queue"] }, { labelsTruncated: true });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "truncated labels -> no dispatch");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("incomplete label evidence")));
	}
	// Missing freshness evidence fails closed.
	{
		const { pi, dashboard, ctx, turn } = await setup(
			{ number: 485, labels: ["3-clanker-queue"] },
			{ missingClosed: true },
		);
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "missing open-state evidence -> no dispatch");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("issue is closed")));
	}
	{
		const { pi, dashboard, ctx, turn } = await setup(
			{ number: 485, labels: ["3-clanker-queue"] },
			{ missingLabelPageInfo: true },
		);
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "missing label pagination evidence -> no dispatch");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("incomplete label evidence")));
	}

	// 11. Wrong returned repository or issue identity
	{
		const { pi, dashboard, ctx, turn } = await setup({ number: 485, labels: ["3-clanker-queue"] }, { wrongRepo: true });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "wrong repo returned -> no dispatch");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("repository mismatch")));
	}
	{
		const { pi, dashboard, ctx, turn } = await setup({ number: 485, labels: ["3-clanker-queue"] }, { wrongNumber: true });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "wrong issue number returned -> no dispatch");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("issue number mismatch")));
	}

	// 12. Missing node
	{
		const { pi, dashboard, ctx, turn } = await setup({ number: 485, labels: ["3-clanker-queue"] }, { missingNode: true });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "missing node -> no dispatch");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("not found")));
	}

	// 13. Mixed batch containing one ineligible Review issue: zero dispatches (do not silently shrink)
	{
		const items = [
			{ number: 485, repo: "projectbluefin/review", labels: ["3-clanker-queue"] }, // eligible
			{ number: 486, repo: "projectbluefin/review", labels: ["hold"] }, // ineligible
		];
		const { pi, dashboard, ctx, turn } = await setup(items);
		// Select all items using 'A'
		dashboard.handleInput("A");
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "batch with one ineligible item yields zero dispatches");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("issue has hold label")));
	}

	// 14. Mixed batch containing Review issue and other repo issue: if Review issue is ineligible, zero dispatches
	{
		const items = [
			{ number: 10, repo: "projectbluefin/other", labels: [] },
			{ number: 485, repo: "projectbluefin/review", labels: [] },
		];
		const { pi, dashboard, ctx, turn } = await setup(items);
		dashboard.handleInput("A");
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "mixed-repo batch with ineligible review issue yields zero dispatches");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("missing explicit admission label")));
	}

	// 15. Unmanaged repository issue: no admission read needed, still dispatches as before
	{
		const { pi, dashboard, turn } = await setup({ number: 936, repo: "projectbluefin/utah", labels: [] });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 1, "unmanaged repository issue dispatches without any admission read");
		assert.match(pi.messages[0], /projectbluefin\/utah#936/);
	}

	// 16. Read-only actions (diff, reference), PR actions, and other issue implementation actions ('fix', 'docs')
	{
		const { pi, dashboard, turn } = await setup({ number: 485, labels: [] });
		pi.messages.length = 0;
		// 'd' for diff — on an issue it inspects the discussion, not the PR-only diff tool
		dashboard.handleInput("d");
		await turn();
		assert.equal(pi.messages.length, 1, "diff is read-only and dispatches without admission gate");
		assert.match(pi.messages[0], /gh issue view 485 --repo projectbluefin\/review --comments/);
	}
	{
		const { dashboard, ctx, turn } = await setup({ number: 485, labels: [] });
		dashboard.handleInput("\r");
		await turn();
		assert.ok(ctx.pasted.length > 0, "cite/reference is read-only");
	}
	// fix on an unadmitted Review issue dispatches zero messages and notifies
	{
		const { pi, dashboard, ctx, turn } = await setup({ number: 485, labels: [] });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "fix on unadmitted Review issue dispatches zero messages");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("missing explicit admission label")));
	}
	// fix on a projectbluefin/review PR is unchanged (no admission read, dispatches)
	{
		const { pi, dashboard, turn } = await setup({ number: 42, type: "pr", repo: "projectbluefin/review", labels: [] }, { isPr: true });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 1, "fix on Review PR dispatches unchanged without admission read");
		assert.match(pi.messages[0], /Fix projectbluefin\/review#42/);
	}

	// 16b. Managed-repository policy is per repository, each with its own vocabulary
	{
		// projectbluefin/documentation is managed with a different admission label
		// and a narrower denied set than projectbluefin/review.
		const { pi, dashboard, turn } = await setup({ number: 21, repo: "projectbluefin/documentation", labels: ["3-docs-queue"] });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 1, "documentation issue admitted on its own queue label");
		assert.match(pi.messages[0], /projectbluefin\/documentation#21/);
	}
	{
		// A review vocabulary label does not admit a documentation issue.
		const { pi, dashboard, ctx, turn } = await setup({ number: 21, repo: "projectbluefin/documentation", labels: ["3-clanker-queue"] });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "review queue label does not admit a documentation issue");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("missing explicit admission label")));
	}
	{
		// A review denied label (blocked) is not in the documentation policy, so it
		// does not block; documentation's own denied label (hold) does.
		const { pi, dashboard, ctx, turn } = await setup({ number: 21, repo: "projectbluefin/documentation", labels: ["3-docs-queue", "hold"] });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "documentation hold label blocks its own dispatch");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("issue has hold label")));
	}
	{
		// Negative fixture in the review repository: hold blocks there too.
		const { pi, dashboard, ctx, turn } = await setup({ number: 485, repo: "projectbluefin/review", labels: ["3-clanker-queue", "hold"] });
		pi.messages.length = 0;
		dashboard.handleInput("f");
		await turn();
		assert.equal(pi.messages.length, 0, "review hold label blocks its own dispatch");
		assert.ok(ctx.notifications.some((n) => n.level === "error" && n.message.includes("issue has hold label")));
	}

	// 17. Selection changes during the read: no retargeting
	{
		const { promise, resolve } = Promise.withResolvers();
		const customFetch = (url, init) => {
			const target = String(url);
			if (target.includes("/graphql")) {
				const body = JSON.parse(String(init?.body ?? "{}"));
				if (body.variables?.search !== undefined) {
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
											number: 485,
											title: "first issue",
											url: "https://github.com/projectbluefin/review/issues/485",
											updatedAt: new Date(NOW - 1000).toISOString(),
											author: { login: "someone" },
											repository: { nameWithOwner: "projectbluefin/review" },
											labels: { nodes: [{ name: "3-clanker-queue" }] },
										},
										{
											number: 486,
											title: "second issue",
											url: "https://github.com/projectbluefin/review/issues/486",
											updatedAt: new Date(NOW - 2000).toISOString(),
											author: { login: "someone" },
											repository: { nameWithOwner: "projectbluefin/review" },
											labels: { nodes: [] },
										},
									],
								},
							},
						}),
					};
				}
				return promise;
			}
			return { ok: true, status: 200, statusText: "OK", json: async () => [] };
		};

		const { pi, dashboard, turn } = await setup([{ number: 485 }, { number: 486 }], { customFetch });
		pi.messages.length = 0;
		dashboard.handleInput(" ");
		// Trigger dispatch for the explicitly selected item 485.
		dashboard.handleInput("f");

		// While admission read is pending, navigate to item 486
		dashboard.handleInput("j");

		// Now resolve the admission read
		resolve({
			ok: true,
			status: 200,
			statusText: "OK",
			json: async () => ({
				data: {
					iss0: {
						nameWithOwner: "projectbluefin/review",
						issue: {
							number: 485,
							closed: false,
							repository: { nameWithOwner: "projectbluefin/review" },
							labels: { pageInfo: { hasNextPage: false }, nodes: [{ name: "3-clanker-queue" }] },
						},
					},
				},
			}),
		});
		await turn();
		assert.equal(pi.messages.length, 1);
		assert.match(pi.messages[0], /projectbluefin\/review#485/, "prompt must dispatch for captured item 485, not navigated item 486");
	}

	// 18. Superseded dispatch generation discard: stale in-flight admission resolution produces no dispatch
	{
		let resolveFirst;
		let resolveSecond;
		let requestCount = 0;
		const { promise: p1, resolve: r1 } = Promise.withResolvers();
		const { promise: p2, resolve: r2 } = Promise.withResolvers();
		resolveFirst = r1;
		resolveSecond = r2;

		const customFetch = (url, init) => {
			const target = String(url);
			if (target.includes("/graphql")) {
				const body = JSON.parse(String(init?.body ?? "{}"));
				if (body.variables?.search !== undefined) {
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
											number: 485,
											title: "first issue",
											url: "https://github.com/projectbluefin/review/issues/485",
											updatedAt: new Date(NOW - 1000).toISOString(),
											author: { login: "someone" },
											repository: { nameWithOwner: "projectbluefin/review" },
											labels: { nodes: [{ name: "3-clanker-queue" }] },
										},
										{
											number: 486,
											title: "second issue",
											url: "https://github.com/projectbluefin/review/issues/486",
											updatedAt: new Date(NOW - 2000).toISOString(),
											author: { login: "someone" },
											repository: { nameWithOwner: "projectbluefin/review" },
											labels: { nodes: [{ name: "3-clanker-queue" }] },
										},
									],
								},
							},
						}),
					};
				}

				requestCount++;
				if (requestCount === 1) {
					return p1;
				}
				return p2;
			}
			return { ok: true, status: 200, statusText: "OK", json: async () => [] };
		};

		const { pi, dashboard, ctx, turn } = await setup([{ number: 485 }, { number: 486 }], { customFetch });
		pi.messages.length = 0;

		// Dispatch 1 on explicitly selected item 485 (pending on p1).
		dashboard.handleInput(" ");
		dashboard.handleInput("f");
		await Promise.resolve();

		// Move to item 486 and start Dispatch 2 on the same persistent workbench.
		dashboard.handleInput("x");
		dashboard.handleInput("j");
		dashboard.handleInput(" ");
		dashboard.handleInput("f");
		await Promise.resolve();
		// Resolve Dispatch 2 first
		resolveSecond({
			ok: true,
			status: 200,
			statusText: "OK",
			json: async () => ({
				data: {
					iss0: {
						nameWithOwner: "projectbluefin/review",
						issue: {
							number: 486,
							closed: false,
							repository: { nameWithOwner: "projectbluefin/review" },
							labels: { pageInfo: { hasNextPage: false }, nodes: [{ name: "3-clanker-queue" }] },
						},
					},
				},
			}),
		});
		await turn();

		assert.equal(pi.messages.length, 1, "Dispatch 2 sends exactly one user message");
		assert.match(pi.messages[0], /projectbluefin\/review#486/);

		// Now let Dispatch 1 resolve late (superseded generation)
		resolveFirst({
			ok: true,
			status: 200,
			statusText: "OK",
			json: async () => ({
				data: {
					iss0: {
						nameWithOwner: "projectbluefin/review",
						issue: {
							number: 485,
							closed: false,
							repository: { nameWithOwner: "projectbluefin/review" },
							labels: { pageInfo: { hasNextPage: false }, nodes: [{ name: "3-clanker-queue" }] },
						},
					},
				},
			}),
		});
		await turn();

		// The stale generation must NOT authorize work or emit an extra message
		assert.equal(pi.messages.length, 1, "stale generation produced zero extra messages; total dispatches across both requests remains 1");
		assert.match(pi.messages[0], /projectbluefin\/review#486/);
	}
});

// OMP workbench mouse/click parity (#462).
test("OMP workbench mouse and click operability matches keyboard actions (#462)", (t) => {
	// Terminal mouse reporting: SGR clicks, releases, and wheel events.
	const sgrClick = parseMouseEvent("\x1b[<0;15;5M");
	assert.deepEqual(sgrClick, { button: 0, col: 14, row: 4, release: false });
	const sgrRelease = parseMouseEvent("\x1b[<0;15;5m");
	assert.deepEqual(sgrRelease, { button: 0, col: 14, row: 4, release: true });
	const sgrWheelUp = parseMouseEvent("\x1b[<64;15;5M");
	assert.equal(sgrWheelUp?.wheel, -1);

	const root = mkdtempSync(join(tmpdir(), "workbench-test-"));
	t.after(() => rmSync(root, { recursive: true, force: true }));
	const mode = new ReviewMode({ org: "projectbluefin" });
	mode.items = [
		queueItem({ id: 7, repo: "projectbluefin/other", title: "feat(ui): dagger rail", ciStatus: "success" }),
		queueItem({ id: 42, repo: "projectbluefin/review", title: "fix(landing): review batches", ciStatus: "failure" }),
		queueItem({ id: 99, repo: "projectbluefin/review", title: "docs: update appliance", ciStatus: "pending" }),
	];
	;

	let lastAction;
	let refreshes = 0;
	const dashboard = new ReviewDashboard(
		{ requestRender() {} },
		PLAIN_PAINTER,
		mode,
		(action) => {
			lastAction = action;
		},
		() => {
			refreshes++;
		},
		22,
	);
	t.after(() => dashboard.dispose());

	// Initial layout and state
	const frame = (w = 220) => dashboard.render(w);
	assert.ok(frame()[0].includes("HIVE WORKBENCH"));
	assert.equal(mode.cursor, 0, "first item selected initially");
	assert.equal(dashboard.currentPane, "queue");

	// 2. Click selection parity: clicking queue row 2 selects item 42
	// Line 3 is item 7, Line 4 is the repo divider for projectbluefin/review,
	// Line 5 is item 42, Line 6 is item 99.
	// Passing SGR mouse sequence: x=10, y=6 (1-based -> line index 5 in 0-based)
	dashboard.handleInput("\x1b[<0;10;6M");
	assert.equal(mode.cursor, 1, "clicking row 2 selects item 42");
	assert.equal(mode.selected().id, 42);
	assert.equal(dashboard.currentPane, "queue");

	// Click row 3 (line index 6, y=7 in SGR): selects item 99
	dashboard.handleInput("\x1b[<0;10;7M");
	assert.equal(mode.cursor, 2, "clicking row 3 selects item 99");
	assert.equal(mode.selected().id, 99);

	// Click divider (line index 4, y=5 in SGR): divider click does not crash or corrupt cursor
	dashboard.handleInput("\x1b[<0;10;5M");
	assert.equal(mode.cursor, 2, "clicking divider preserves selection");

	// Click row 1 (line index 3, y=4 in SGR): selects item 7
	dashboard.handleInput("\x1b[<0;10;4M");
	assert.equal(mode.cursor, 0, "clicking row 1 selects item 7");
	assert.equal(mode.selected().id, 7);

	// Click checkbox on row 1 (columns 1..3, row 4 in 1-based coordinates)
	assert.equal(mode.selectedKeys.has("projectbluefin/other#7"), false);
	dashboard.handleInput("\x1b[<0;3;4M");
	assert.equal(mode.selectedKeys.has("projectbluefin/other#7"), true, "clicking checkbox selects item");
	assert.ok(frame().some((r) => r.includes("☒")));

	// Clicking checkbox again toggles it off
	dashboard.handleInput("\x1b[<0;3;4M");
	assert.equal(mode.selectedKeys.has("projectbluefin/other#7"), false, "clicking checkbox again deselects item");

	// 3. Panes focus parity: clicking trace pane focuses trace pane
	// Trace pane starts at col half+ (right side of split layout)
	dashboard.handleInput("\x1b[<0;151;4M");
	assert.equal(dashboard.currentPane, "trace", "clicking right pane focuses trace");
	assert.equal(dashboard.activePane, "trace");

	// Clicking queue pane focuses queue pane back
	dashboard.handleInput("\x1b[<0;15;4M");
	assert.equal(dashboard.currentPane, "queue", "clicking left pane focuses queue");

	// 4. Traces toggle parity: expanding and collapsing trace spans on click
	dashboard.handleInput("\x1b[<0;151;4M"); // focus trace pane
	const beforeFoldFrame = frame();
	// Click a trace span row (e.g. row 5 in trace pane)
	dashboard.handleClick(150, 5);
	const afterFoldFrame = frame();
	assert.ok(afterFoldFrame.length > 0);
	// Clicking again toggles expansion back
	dashboard.handleClick(150, 5);

	// 5. Mouse scrolling parity (wheel up/down)
	dashboard.handleClick(15, 4); // focus queue pane
	assert.equal(mode.cursor, 0);
	// Wheel down moves down 1 item
	dashboard.handleInput("\x1b[<65;15;5M");
	assert.equal(mode.cursor, 1, "wheel down moves cursor to next item");
	// Wheel down again
	dashboard.handleInput("\x1b[<65;15;5M");
	assert.equal(mode.cursor, 2, "wheel down moves cursor to 3rd item");
	// Wheel up moves back
	dashboard.handleInput("\x1b[<64;15;5M");
	assert.equal(mode.cursor, 1, "wheel up moves cursor up");

	// 6. Every visible keymap action has mouse parity.
	const lines = dashboard.render(400);
	const keymapLineIdx = lines.findIndex((line) => line.includes("slay") && line.includes("comment"));
	assert.ok(keymapLineIdx > 0, "keymap bar rendered");
	const keymapText = lines[keymapLineIdx];

	const sPos = keymapText.indexOf("s slay");
	assert.ok(sPos > 0);
	dashboard.handleClick(sPos + 1, keymapLineIdx);
	assert.equal(lastAction?.kind, "slay", "clicking slay dispatches autoreview");

	const cPos = keymapText.indexOf("c comment");
	assert.ok(cPos > 0);
	dashboard.handleClick(cPos + 1, keymapLineIdx);
	assert.equal(lastAction?.kind, "comment", "clicking comment emits a comment action");

	const dPos = keymapText.indexOf("d diff");
	assert.ok(dPos > 0);
	dashboard.handleClick(dPos + 1, keymapLineIdx);
	assert.equal(lastAction?.kind, "diff", "clicking diff emits a diff action");

	const fPos = keymapText.indexOf("f fix");
	assert.ok(fPos > 0);
	dashboard.handleClick(fPos + 1, keymapLineIdx);
	assert.equal(lastAction?.kind, "fix", "clicking fix emits a non-landing work action");

	const enterPos = keymapText.indexOf("enter cite");
	assert.ok(enterPos > 0);
	dashboard.handleClick(enterPos + 1, keymapLineIdx);
	assert.equal(lastAction?.kind, "reference", "clicking cite emits a reference action");

	const APos = keymapText.indexOf("A all");
	assert.ok(APos > 0);
	dashboard.handleClick(APos + 1, keymapLineIdx);
	assert.equal(mode.selectedKeys.size, 3, "clicking all selects every visible item");
	const xPos = keymapText.indexOf("x clear");
	assert.ok(xPos > 0);
	dashboard.handleClick(xPos + 1, keymapLineIdx);
	assert.equal(mode.selectedKeys.size, 0, "clicking clear clears selection");

	const oPos = keymapText.indexOf("o repo");
	assert.ok(oPos > 0);
	dashboard.handleClick(oPos + 1, keymapLineIdx);
	assert.equal(lastAction?.kind, "scope", "clicking repo opens the scope selector");

	const tPos = keymapText.indexOf("t trace");
	assert.ok(tPos > 0);
	const paneBefore = dashboard.currentPane;
	dashboard.handleClick(tPos + 1, keymapLineIdx);
	assert.notEqual(dashboard.currentPane, paneBefore, "clicking t switches pane");

	const tabPos = keymapText.indexOf("tab prs/issues");
	assert.ok(tabPos > 0);
	const modeBefore = mode.queueMode;
	const itemsBefore = [...mode.items];
	dashboard.handleClick(tabPos + 1, keymapLineIdx);
	assert.notEqual(mode.queueMode, modeBefore, "clicking Tab toggles queue mode");
	dashboard.handleClick(tabPos + 1, keymapLineIdx);
	assert.equal(mode.queueMode, modeBefore);
	mode.items = itemsBefore;
	;

	const HPos = keymapText.indexOf("H hive");
	assert.ok(HPos > 0);
	const hiveBefore = mode.hiveOnly;
	dashboard.handleClick(HPos + 1, keymapLineIdx);
	assert.notEqual(mode.hiveOnly, hiveBefore, "clicking H toggles Hive-only filtering");
	dashboard.handleClick(HPos + 1, keymapLineIdx);
	assert.equal(mode.hiveOnly, hiveBefore);

	// Click 'close' (q)
	const qPos = keymapText.indexOf("q close");
	assert.ok(qPos > 0);
	dashboard.handleClick(qPos + 1, keymapLineIdx);
	assert.equal(lastAction?.kind, "close", "clicking close chord triggers close action");

	// 7. Active-turn concurrency: actions remain available during active turns
	mode.session.startTurn(NOW);
	mode.session.startTool("active-turn-tool", "bash", { command: "just review-check" }, NOW + 10);
	;
	assert.ok(mode.session.active() !== undefined, "active tool turn in flight");

	// During active turn: click selects row, expands trace, and triggers actions
	dashboard.handleClick(15, 3);
	assert.equal(mode.cursor, 0, "active turn allows click row selection");
	dashboard.handleClick(250, 4);
	assert.equal(dashboard.currentPane, "trace", "active turn allows pane focus");
	dashboard.handleClick(sPos + 1, keymapLineIdx);
	assert.equal(lastAction?.kind, "slay", "active turn allows slay dispatch");
	mode.session.endTool("active-turn-tool", { content: [{ type: "text", text: "done" }] }, false, NOW + 100);
	mode.session.endTurn(NOW + 200);

	// 8. Header and status bar clicks
	// Header row: clicking mode area toggles mode
	dashboard.handleClick(25, 0);
	assert.equal(mode.queueMode, "issues");
	dashboard.handleClick(25, 0);
	assert.equal(mode.queueMode, "prs");
	mode.items = itemsBefore;
	;

	// 9. Narrow terminal stacked layout click parity
	const narrowFrame = dashboard.render(70);
	assert.ok(narrowFrame.some((r) => r.startsWith("▼ QUEUE") || r.startsWith("▶ QUEUE")));
	// Click queue row in narrow layout (row 3 is item 7, row 4 is divider, row 5 is item 42)
	dashboard.handleClick(15, 3);
	assert.equal(mode.cursor, 0);
	dashboard.handleClick(15, 4);
	assert.equal(mode.cursor, 0, "clicking divider in stacked layout preserves selection");
	dashboard.handleClick(15, 5);
	assert.equal(mode.cursor, 1, "narrow stacked layout selects row on click");
});

// Bounded adaptive tool fallback: the queue is two sources, one truth. Search
// decides what is nearby; the by-name lookup repairs what Hive requires and is
// outside the window; the shortfall is reported instead of a row that merely
// looks complete. From the repaired queue the agent reaches a typed tool, reads
// live checks, and stops at the merge line.
test("bounded fallback repairs by name, observes live checks, and never merges", async (t) => {
	const root = mkdtempSync(join(tmpdir(), "workbench-test-"));
	t.after(() => rmSync(root, { recursive: true, force: true }));

	const listState = () =>
		(readdirSync(root, { recursive: true }) as string[]).sort();
	const before = listState();

	// Search only surfaces one nearby PR. Hive's issue-only work must not be
	// looked up as a pull request, while its explicit open linked PR can still
	// arrive by identity. A direct Hive PR that is closed remains a shortfall.
	const byName = {
		"projectbluefin/review#937": {
			number: 937,
			title: "hold: gate the release behind soak",
			url: "https://github.com/projectbluefin/review/pull/937",
			updatedAt: new Date(NOW - 5 * 24 * 3600 * 1000).toISOString(),
			isDraft: false,
			mergeable: "MERGEABLE",
			reviewDecision: "REVIEW_REQUIRED",
			additions: 0,
			deletions: 0,
			author: { login: "castrojo" },
			repository: { nameWithOwner: "projectbluefin/review" },
			labels: { nodes: [{ name: "hold" }, { name: "review_required" }] },
			commits: { nodes: [{ commit: { statusCheckRollup: { state: "PENDING" } } }] },
			closingIssuesReferences: {
				nodes: [{ number: 936, repository: { nameWithOwner: "projectbluefin/review" } }],
			},
		},
	};
	const lookupQueries: string[] = [];

	const fetchImpl = async (url, init) => {
		const target = String(url);
		if (target.includes("/graphql")) {
			const body = JSON.parse(String(init?.body ?? "{}"));
			if (body.variables?.search) {
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
								],
							},
						},
					}),
				};
			}
			lookupQueries.push(body.query);
			// Native gh repair: one aliased by-name lookup for what search missed.
			const data = {};
			const lookup = /(\w+): repository\(owner: "([^"]+)", name: "([^"]+)"\)\s*\{\s*issueOrPullRequest\(number: (\d+)\)/g;
			for (const m of body.query.matchAll(lookup)) {
				const w = m[1], owner = m[2], name = m[3], number = m[4];
				data[w] = { issueOrPullRequest: byName[`${owner}/${name}#${number}`] ?? null };
			}
			return { ok: true, status: 200, statusText: "OK", json: async () => ({ data }) };
		}
		if (target.includes("/api/v1/status")) {
			return { ok: true, status: 200, statusText: "OK", json: async () => ({ hub: "online", actionable_items: 12 }) };
		}
		if (target.includes("/api/contribute/queue")) {
			return {
				ok: true,
				status: 200,
				statusText: "OK",
				json: async () => ({
					queue: [
						{ repo: "projectbluefin/review", number: 42, title: "fix(launcher)", url: "https://github.com/projectbluefin/review/pull/42", updatedAt: new Date(NOW - 1000).toISOString(), author: { login: "jorge" }, repository: { nameWithOwner: "projectbluefin/review" }, labels: { nodes: [] }, closed: false },
						{ repo: "projectbluefin/review", number: 936, title: "hold: gate the release", url: "https://github.com/projectbluefin/review/issues/936", updatedAt: new Date(NOW - 5 * 24 * 3600 * 1000).toISOString(), author: { login: "castrojo" }, repository: { nameWithOwner: "projectbluefin/review" }, labels: { nodes: [] }, closed: false, pr: { number: 937, url: "https://github.com/projectbluefin/review/pull/937", state: "open" } },
						{ repo: "projectbluefin/review", number: 470, title: "ship the launcher", url: "https://github.com/projectbluefin/review/pull/470", updatedAt: new Date(NOW - 2 * 24 * 3600 * 1000).toISOString(), author: { login: "ada" }, repository: { nameWithOwner: "projectbluefin/review" }, labels: { nodes: [] }, closed: true },
					],
				}),
			};
		}
		if (target.includes("/api/contribute/triage") || target.includes("/api/v1/contributors")) {
			return { ok: true, status: 200, statusText: "OK", json: async () => ({ groups: [], contributors: [] }) };
		}
		if (target.includes("/pulls/")) {
			// The available typed tool, reached natively — no browser in the path.
			return { ok: true, status: 200, statusText: "OK", json: async () => [
				{ filename: "image/entrypoint.sh", status: "modified", additions: 3, deletions: 1, patch: "@@ -1 +1 @@\n-old\n+new" },
			] };
		}
		return { ok: true, status: 404, statusText: "Not Found", json: async () => ({}) };
	};

	const pi = fakeHost();
	const review = createReviewExtension(pi, {
		org: "projectbluefin",
		fetchImpl: fetchImpl as unknown as typeof fetch,
		env: { ...ISOLATED_ENV, HIVE_HUB: "https://hive.example" },
	});
	const ctx = fakeCtx();
	ctx.ui.parent = ctx;
	;
	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();

	// The fallback repairs only mode-matching identities: the nearby PR and the
	// linked PR are present, the issue-only key was never queried as a PR, and
	// the closed direct PR is reported as a shortfall.
	const queue = await pi.tools.get("hive_workbench_queue").execute("id", {});
	assert.match(queue.content[0].text, /projectbluefin\/review#42/);
	assert.match(queue.content[0].text, /projectbluefin\/review#937/, "the linked PR arrives by native repair");
	assert.doesNotMatch(queue.content[0].text, /#936/, "the source issue is not rendered as a pull request");
	assert.doesNotMatch(queue.content[0].text, /#470/, "the closed pull request is a shortfall, not a row");
	assert.equal(lookupQueries.length, 1);
	assert.doesNotMatch(lookupQueries[0]!, /number: 936/, "issue-only keys are not queried as pull requests");
	assert.match(lookupQueries[0]!, /number: 937/, "the explicit open linked pull request is queried");
	assert.equal(queue.details.order_source, "hive");

	// Queue repair is in-memory only: it writes no files.
	assert.deepEqual(listState(), before, "repair made no on-disk changes");

	// The agent reaches a typed tool from the repaired queue and reads the real
	// bounded diff — native gh, not a browser — honest about its bounds.
	const diff = await pi.tools.get("hive_workbench_diff").execute("id", { pull_request: 42, repo: "projectbluefin/review" });
	assert.match(diff.content[0].text, /entrypoint\.sh/);
	assert.match(diff.content[0].text, /\+3 -1/);
	assert.equal(diff.isError, false);

	// The status tool observes live checks and names the queue authority.
	// The selected item's review state is a blocker the tool reports.
	const status = await pi.tools.get("hive_workbench_status").execute("id", {});
	assert.match(status.content[0].text, /ci: 0 passing, 1 failing, 1 pending/);
	assert.match(status.content[0].text, /review_required/);
	assert.match(status.content[0].text, /missing from this queue/, "the unresolved item is reported, not hidden");
	assert.equal(status.details.order_source, "hive");
	assert.equal(status.details.hive.queued.present, 2);
	assert.equal(status.details.hive.queued.total, 3, "only mode-matching Hive work contributes to coverage");

	// The queue refreshes without duplicating the repaired rows.
	(pi.shortcuts.get("alt+u") as { handler: (ctx: unknown) => void }).handler(ctx);
	await review.whenStarted();
	const queue2 = await pi.tools.get("hive_workbench_queue").execute("id", {});
	const occurrences = queue2.content[0].text.match(/projectbluefin\/review#937/g) ?? [];
	assert.equal(occurrences.length, 1, "a refresh does not re-add a repaired row");
});

test("fix button dispatches workflowz wave for selected issues without requiring Hive", async () => {
	const issues = [
		{ number: 101, title: "first bug", repo: "projectbluefin/unmanaged" },
		{ number: 102, title: "second bug", repo: "projectbluefin/unmanaged" },
	];
	const fetchImpl = async (url: string | URL | Request, init?: RequestInit) => {
		const target = String(url);
		if (target.includes("/graphql")) {
			return {
				ok: true,
				status: 200,
				statusText: "OK",
				json: async () => ({
					data: {
						search: {
							pageInfo: { hasNextPage: false, endCursor: null },
							nodes: issues.map((it) => ({
								number: it.number,
								title: it.title,
								url: `https://github.com/${it.repo}/issues/${it.number}`,
								updatedAt: new Date(NOW - 1000).toISOString(),
								author: { login: "someone" },
								repository: { nameWithOwner: it.repo },
								labels: { nodes: [] },
							})),
						},
					},
				}),
			};
		}
		return { ok: true, status: 200, statusText: "OK", json: async () => [] };
	};

	const pi = fakeHost();
	pi.flagValues.set("issues", true);
	const review = createReviewExtension(pi, {
		org: "projectbluefin",
		fetchImpl: fetchImpl as unknown as typeof fetch,
		env: ISOLATED_ENV, // No HIVE_HUB -> Hive is not configured / offline
	});
	const ctx = fakeCtx();
	ctx.ui.parent = ctx;

	await pi.events.get("session_start")({}, ctx);
	await review.whenStarted();
	const dashboard = ctx.overlays[0] as unknown as ReviewDashboard;

	// Select both issues using Space on row 0, Down, Space on row 1
	dashboard.handleInput(" ");
	dashboard.handleInput("j");
	dashboard.handleInput(" ");

	// Press 'f' to fix all selected issues
	pi.messages.length = 0;
	dashboard.handleInput("f");

	// Yield event loop
	for (let i = 0; i < 20; i++) await Promise.resolve();

	assert.equal(pi.messages.length, 1, "selected issues dispatched without requiring Hive");
	assert.match(pi.messages[0], /Implement this repository wave for projectbluefin\/unmanaged/);
	assert.match(pi.messages[0], /Use the `task` tool once with one fresh isolated item/);
	assert.match(pi.messages[0], /one review-ready pull request per issue/);
	assert.match(pi.messages[0], /SUBAGENT-RULES/);
	assert.match(pi.messages[0], /Never merge or approve your own pull request/);
	assert.equal(
		ctx.notifications.some((n) => n.message.includes("Hive is unavailable")),
		false,
		"must not block with 'Hive is unavailable'",
	);
});

const flush = () => new Promise((r) => setTimeout(r, 0));

test("v opens a mode-aware reader for both issue and PR rows", (t) => {
	const mode = new ReviewMode({ org: "projectbluefin" });
	mode.items = [
		queueItem({ id: 611, type: "issue", title: "issue rows advertise a reader", url: "https://github.com/projectbluefin/review/issues/611" }),
		queueItem({ id: 42, type: "pr", title: "fix(launcher): resolve HIVE_HUB before mutating", url: "https://github.com/projectbluefin/review/pull/42" }),
	];
	let action;
	const dashboard = new ReviewDashboard({ requestRender() {} }, PLAIN_PAINTER, mode, (result) => { action = result; }, () => {}, 160);
	t.after(() => dashboard.dispose());
	const frame = () => dashboard.render(160);

	// An issue row opens the ISSUE READER, distinct from the PR reader.
	mode.selectById("projectbluefin/review", 611);
	dashboard.handleInput("v");
	const issueFrame = frame();
	assert.ok(issueFrame.some((r) => r.includes("ISSUE READER: projectbluefin/review#611 — issue rows advertise a reader")), "issue row opens the issue reader");
	assert.ok(issueFrame.some((r) => r.includes("loading description, conversation, and linked pull requests")), "issue reader loads its own fields");
	assert.ok(!issueFrame.some((r) => r.includes("PR READER")), "the issue reader is not the PR reader");
	assert.equal(action, undefined, "opening the reader starts no agent turn or mutation");

	// A PR row opens the PR READER.
	mode.selectById("projectbluefin/review", 42);
	dashboard.handleInput("v");
	const prFrame = frame();
	assert.ok(prFrame.some((r) => r.includes("PR READER: projectbluefin/review#42 — fix(launcher): resolve HIVE_HUB before mutating")), "PR row opens the PR reader");
	assert.ok(prFrame.some((r) => r.includes("loading description and conversation")), "PR reader loads its own fields");
	assert.equal(action, undefined, "opening the reader starts no agent turn or mutation");
});

test("the issue reader renders a populated issue without touching the PR diff", async (t) => {
	const mode = new ReviewMode({ org: "projectbluefin" });
	mode.items = [
		queueItem({ id: 611, type: "issue", title: "issue reader", url: "https://github.com/projectbluefin/review/issues/611" }),
	];
	mode.token = "test-token";
	const calls: string[] = [];
	const originalFetch = globalThis.fetch;
	globalThis.fetch = (async (url: string) => {
		calls.push(String(url));
		const s = String(url);
		if (s.includes("/issues/611/comments")) return { ok: true, status: 200, statusText: "OK", json: async () => [{ user: { login: "ada" }, created_at: "2026-01-01", body: "agreed" }] };
		if (s.includes("/issues/611/timeline")) return { ok: true, status: 200, statusText: "OK", json: async () => [{ event: "cross_referenced", source: { issue: { number: 12, title: "boot KDE", state: "open", url: "https://github.com/projectbluefin/review/pull/12", pull_request: {} } } }] };
		return { ok: true, status: 200, statusText: "OK", json: async () => ({ title: "issue reader", body: "Adds an issue reader.", user: { login: "joshyorko" }, state: "open", labels: [{ name: "bug" }], url: "https://github.com/projectbluefin/review/issues/611" }) };
	}) as typeof fetch;
	t.after(() => { globalThis.fetch = originalFetch; });

	const dashboard = new ReviewDashboard({ requestRender() {} }, PLAIN_PAINTER, mode, () => {}, () => {}, 160);
	t.after(() => dashboard.dispose());

	mode.selectById("projectbluefin/review", 611);
	dashboard.handleInput("v");
	await flush();

	const frame = frameAfter(dashboard);
	assert.ok(frame.some((r) => r.includes("Adds an issue reader.")), "the issue body is rendered");
	assert.ok(frame.some((r) => r.includes("@ada")), "the issue conversation is rendered");
	assert.ok(frame.some((r) => r.includes("#12 [open] boot KDE")), "a linked pull request is rendered");
	assert.ok(!calls.some((u) => u.includes("/pulls/611/files")), "the issue reader never calls the PR diff/files endpoint");

	// Refreshing re-fetches and keeps the issue reader populated (not the stale
	// loading state), proving the issue detail cache is wired like the PR cache.
	mode.selectById("projectbluefin/review", 611);
	dashboard.handleInput("r");
	await flush();
	assert.ok(frameAfter(dashboard).some((r) => r.includes("Adds an issue reader.")), "refreshing the issue keeps the issue reader cached");
});

const frameAfter = (dashboard: { render: (w: number) => string[] }) => dashboard.render(160);
