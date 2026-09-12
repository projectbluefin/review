/**
 * Durable Bluefin state, read back as spans.
 *
 * The review appliance already records everything the dashboard needs under
 * `${XDG_STATE_HOME:-~/.local/state}/bluefin-review/`: the exact-head run state
 * machine, per-batch review events, landing lifecycle events, and review receipts.
 * This module reads those artifacts and nothing else — it never writes, never
 * decides, and never invents a stage that did not happen.
 *
 * Reads are bounded on purpose: a landing log grows without limit, and the
 * dashboard repaints on a timer.
 */

import { closeSync, existsSync, fstatSync, openSync, readFileSync, readSync, readdirSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { Span } from "./trace.ts";
import { GLYPH, type SpanStatus } from "./glyphs.ts";

/** Bytes read from the tail of a JSONL log. */
const TAIL_BYTES = 64 * 1024;
/** Most recent JSONL logs consulted per directory. */
const MAX_LOGS = 12;

export interface RunRecord {
	repository: string;
	pullRequest: number;
	headSha: string;
	backend: string;
	model: string;
	effort: string;
	state: string;
	reason: string;
	retryAt: string;
	createdAt: number;
	updatedAt: number;
	sequence: number;
}

export interface ReviewEvent {
	/** `owner/repo#number`. */
	key: string;
	state: string;
	note: string;
	timestamp: number;
	receipt?: string;
	headSha?: string;
}

export interface LandingEvent {
	pullRequestKey?: string;
	state?: string;
	note: string;
	timestamp: number;
	done?: boolean;
	watchStatus?: string;
	headSha?: string;
}
export interface Finding {
	severity: "critical" | "high" | "medium" | "low";
	file: string;
	line: number;
	title: string;
}

export interface Verification {
	name: string;
	state: "verified" | "unverified" | "skipped";
	evidence: string;
}

export interface Receipt {
	repository: string;
	pullRequest: number;
	headSha: string;
	backend: string;
	model: string;
	state: string;
	counts: Record<string, number>;
	findings: Finding[];
	verification: Verification[];
	createdAt?: number;
}

export interface StateSnapshot {
	root: string;
	runs: RunRecord[];
	reviewEvents: ReviewEvent[];
	landingEvents: LandingEvent[];
	receipts: Map<string, Receipt>;
	/** Populated when the state root exists but could not be read. */
	error?: string;
}

/** Resolve the appliance state root the same way the Python side does. */
export function stateRoot(env: NodeJS.ProcessEnv = process.env): string {
	const base = env.XDG_STATE_HOME && env.XDG_STATE_HOME.length > 0 ? env.XDG_STATE_HOME : join(homedir(), ".local", "state");
	return join(base, "bluefin-review");
}

export function queueKey(repository: string, pullRequest: number): string {
	return `${repository}#${pullRequest}`;
}

function readJson(path: string): unknown {
	try {
		return JSON.parse(readFileSync(path, "utf8"));
	} catch {
		return undefined;
	}
}

/** Read the last `TAIL_BYTES` of a file, dropping a partial leading line. */
function readTail(path: string): string {
	let fd: number | undefined;
	try {
		fd = openSync(path, "r");
		const size = fstatSync(fd).size;
		const length = Math.min(size, TAIL_BYTES);
		const buffer = Buffer.allocUnsafe(length);
		readSync(fd, buffer, 0, length, size - length);
		const text = buffer.toString("utf8");
		return size > length ? text.slice(text.indexOf("\n") + 1) : text;
	} catch {
		return "";
	} finally {
		if (fd !== undefined) closeSync(fd);
	}
}

function recentFiles(dir: string, suffix: string, limit: number): string[] {
	try {
		return readdirSync(dir)
			.filter((name) => name.endsWith(suffix))
			.map((name) => {
				const path = join(dir, name);
				try {
					return { path, mtime: statSync(path).mtimeMs };
				} catch {
					return { path, mtime: 0 };
				}
			})
			.sort((a, b) => b.mtime - a.mtime)
			.slice(0, limit)
			.map((entry) => entry.path);
	} catch {
		return [];
	}
}

function parseLines(text: string): Record<string, unknown>[] {
	const rows: Record<string, unknown>[] = [];
	for (const line of text.split("\n")) {
		const trimmed = line.trim();
		if (!trimmed.startsWith("{")) continue;
		try {
			const value = JSON.parse(trimmed);
			if (value && typeof value === "object") rows.push(value as Record<string, unknown>);
		} catch {
			// A torn write at the tail of a live log is expected, not an error.
		}
	}
	return rows;
}

function asNumber(value: unknown): number | undefined {
	return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function asString(value: unknown): string {
	return typeof value === "string" ? value : "";
}

/** Appliance timestamps are epoch seconds; the renderer works in milliseconds. */
function toMillis(seconds: unknown): number {
	const value = asNumber(seconds);
	return value === undefined ? 0 : value * 1000;
}

export function readRunRecords(root: string): RunRecord[] {
	const payload = readJson(join(root, "run-state", "run-state.json")) as
		| { version?: number; records?: unknown[] }
		| undefined;
	if (!payload || !Array.isArray(payload.records)) return [];

	const records: RunRecord[] = [];
	for (const raw of payload.records) {
		if (!raw || typeof raw !== "object") continue;
		const record = raw as Record<string, unknown>;
		const identity = (record.identity ?? {}) as Record<string, unknown>;
		const pullRequest = asNumber(identity.pull_request);
		if (pullRequest === undefined) continue;
		records.push({
			repository: asString(identity.repository),
			pullRequest,
			headSha: asString(identity.head_sha),
			backend: asString(identity.backend),
			model: asString(identity.model),
			effort: asString(identity.effort),
			state: asString(record.state),
			reason: asString(record.reason),
			retryAt: asString(record.retry_at),
			createdAt: toMillis(record.created_at),
			updatedAt: toMillis(record.updated_at),
			sequence: asNumber(record.sequence) ?? 0,
		});
	}
	return records.sort((a, b) => a.sequence - b.sequence);
}

export function readReviewEvents(root: string): ReviewEvent[] {
	const events: ReviewEvent[] = [];
	for (const path of recentFiles(join(root, "review-batches"), ".jsonl", MAX_LOGS)) {
		for (const row of parseLines(readTail(path))) {
			const key = asString(row.key);
			if (!key) continue;
			events.push({
				key,
				state: asString(row.state),
				note: asString(row.note),
				timestamp: toMillis(row.ts),
				receipt: typeof row.receipt === "string" ? row.receipt : undefined,
				headSha: typeof row.head_sha === "string" ? row.head_sha : undefined,
			});
		}
	}
	return events.sort((a, b) => a.timestamp - b.timestamp);
}

export function readLandingEvents(root: string): LandingEvent[] {
	const events: LandingEvent[] = [];
	for (const path of recentFiles(join(root, "landings"), ".jsonl", MAX_LOGS)) {
		for (const row of parseLines(readTail(path))) {
			const watch = row.watch as Record<string, unknown> | undefined;
			const watchRepository = watch ? asString(watch.repository) : "";
			const watchNumber = watch ? asNumber(watch.pull_request) : undefined;
			const head = typeof row.head === "string" ? row.head : typeof row.head_sha === "string" ? row.head_sha : (watch && typeof watch.head_sha === "string" ? watch.head_sha : undefined);
			events.push({
				pullRequestKey:
					typeof row.pr === "string"
						? row.pr
						: watchRepository && watchNumber !== undefined
							? queueKey(watchRepository, watchNumber)
							: undefined,
				state: typeof row.state === "string" ? row.state : undefined,
				note: asString(row.note),
				timestamp: toMillis(row.ts),
				done: row.done === true,
				watchStatus: watch ? asString(watch.status) : undefined,
				headSha: head,
			});
		}
	}
	return events.sort((a, b) => a.timestamp - b.timestamp);
}

function parseReceipt(payload: unknown): Receipt | undefined {
	if (!payload || typeof payload !== "object") return undefined;
	const record = payload as Record<string, unknown>;
	const identity = (record.identity ?? {}) as Record<string, unknown>;
	const analysis = (record.analysis ?? {}) as Record<string, unknown>;
	const pullRequest = asNumber(identity.pull_request);
	if (pullRequest === undefined) return undefined;

	const findings: Finding[] = [];
	for (const raw of Array.isArray(analysis.findings) ? analysis.findings : []) {
		if (!raw || typeof raw !== "object") continue;
		const finding = raw as Record<string, unknown>;
		const severity = asString(finding.severity);
		if (severity !== "critical" && severity !== "high" && severity !== "medium" && severity !== "low") continue;
		findings.push({
			severity,
			file: asString(finding.file),
			line: asNumber(finding.line) ?? 0,
			title: asString(finding.title),
		});
	}

	const verification: Verification[] = [];
	for (const raw of Array.isArray(analysis.verification) ? analysis.verification : []) {
		if (!raw || typeof raw !== "object") continue;
		const item = raw as Record<string, unknown>;
		const state = asString(item.state);
		if (state !== "verified" && state !== "unverified" && state !== "skipped") continue;
		verification.push({ name: asString(item.name), state, evidence: asString(item.evidence) });
	}

	const counts: Record<string, number> = {};
	if (analysis.counts && typeof analysis.counts === "object") {
		for (const [severity, value] of Object.entries(analysis.counts as Record<string, unknown>)) {
			const count = asNumber(value);
			if (count !== undefined) counts[severity] = count;
		}
	}

	const createdAt = Date.parse(asString(record.created_at));
	return {
		repository: asString(identity.repository),
		pullRequest,
		headSha: asString(identity.head_sha),
		backend: asString(identity.backend),
		model: asString(identity.model),
		state: asString(analysis.state),
		counts,
		findings,
		verification,
		createdAt: Number.isNaN(createdAt) ? undefined : createdAt,
	};
}

/** Newest receipt per `owner/repo#number`, from the review cache directory. */
export function readReceipts(root: string, limit = 24): Map<string, Receipt> {
	const receipts = new Map<string, Receipt>();
	for (const path of recentFiles(join(root, "reviews"), ".json", limit)) {
		const receipt = parseReceipt(readJson(path));
		if (!receipt) continue;
		const key = queueKey(receipt.repository, receipt.pullRequest);
		if (!receipts.has(key)) receipts.set(key, receipt);
	}
	return receipts;
}

/** One bounded read of every durable source the dashboard projects. */
export function readStateSnapshot(root = stateRoot()): StateSnapshot {
	if (!existsSync(root)) {
		return { root, runs: [], reviewEvents: [], landingEvents: [], receipts: new Map() };
	}
	try {
		return {
			root,
			runs: readRunRecords(root),
			reviewEvents: readReviewEvents(root),
			landingEvents: readLandingEvents(root),
			receipts: readReceipts(root),
		};
	} catch (error) {
		return {
			root,
			runs: [],
			reviewEvents: [],
			landingEvents: [],
			receipts: new Map(),
			error: error instanceof Error ? error.message : String(error),
		};
	}
}

/**
 * Cheap change token for a snapshot.
 *
 * The dashboard polls durable state on a timer; repainting a terminal because a
 * poll happened, rather than because something moved, is how a TUI ends up
 * burning a core while idle.
 */
export function snapshotSignature(snapshot: StateSnapshot): string {
	let runs = 0;
	for (const run of snapshot.runs) runs = Math.max(runs, run.updatedAt);
	const lastReview = snapshot.reviewEvents[snapshot.reviewEvents.length - 1];
	const lastLanding = snapshot.landingEvents[snapshot.landingEvents.length - 1];
	return [
		snapshot.runs.length,
		runs,
		snapshot.reviewEvents.length,
		lastReview ? `${lastReview.key}:${lastReview.state}:${lastReview.timestamp}` : "",
		snapshot.landingEvents.length,
		lastLanding ? `${lastLanding.pullRequestKey}:${lastLanding.state}:${lastLanding.timestamp}` : "",
		snapshot.receipts.size,
	].join("|");
}

/**
 * Did a recorded review report findings for this item?
 *
 * Any of the three durable sources counts: the receipt is the verdict, the run
 * state machine is the exact-head record, and the batch event stream is what the
 * engine last emitted. A maintainer needs to know that something was found, not
 * which file recorded it first.
 */
export function hasRecordedFindings(snapshot: StateSnapshot, key: string): boolean {
	const receipt = snapshot.receipts.get(key);
	if (receipt && (receipt.state === "findings" || receipt.findings.length > 0)) return true;

	for (const run of snapshot.runs) {
		if (queueKey(run.repository, run.pullRequest) !== key) continue;
		if (run.state === "review_findings" || run.state === "escalation_required") return true;
	}

	for (let index = snapshot.reviewEvents.length - 1; index >= 0; index--) {
		const event = snapshot.reviewEvents[index]!;
		if (event.key !== key) continue;
		return event.state === "findings";
	}
	return false;
}

/** `run_state.RunState` → span status. */
export function runStateStatus(state: string): SpanStatus {
	switch (state) {
		case "pending":
		case "retry_at":
			return "pending";
		case "reviewing":
		case "re_reviewing":
		case "mutating":
			return "running";
		case "review_clean":
		case "completed":
			return "success";
		case "review_findings":
		case "escalation_required":
		case "blocked":
		case "human_review_missing":
			return "findings";
		default:
			return "failure";
	}
}

/** `review_engine.ReviewEvent` state → span status. */
export function reviewEventStatus(state: string): SpanStatus {
	switch (state) {
		case "running":
			return "running";
		case "cached":
			return "cached";
		case "complete":
			return "success";
		case "findings":
			return "findings";
		case "cancelled":
			return "skipped";
		default:
			return "failure";
	}
}

/** `landing.PR_STATES` → span status. */
export function landingStateStatus(state: string): SpanStatus {
	switch (state) {
		case "diagnosing":
		case "fixing":
		case "waiting-ci":
		case "merging":
		case "awaiting-stable":
			return "running";
		case "merged":
		case "pr-opened":
		case "finding-filed":
			return "success";
		case "blocked":
			return "findings";
		default:
			return "failure";
	}
}

/** A phase the log has moved past cannot still be running. */
function settledStatus(status: SpanStatus): SpanStatus {
	return status === "running" ? "success" : status;
}

const SEVERITY_STATUS: Record<Finding["severity"], SpanStatus> = {
	critical: "failure",
	high: "failure",
	medium: "findings",
	low: "findings",
};

function severitySummary(counts: Record<string, number>): string {
	const parts: string[] = [];
	for (const severity of ["critical", "high", "medium", "low"]) {
		const count = counts[severity] ?? 0;
		if (count > 0) parts.push(`${count} ${severity}`);
	}
	return parts.join(", ");
}

/**
 * Project everything known about one queue item into a Dagger-shaped trace.
 *
 * Stage order is the appliance's own: the run state machine owns the head, the
 * review engine reports its verdict, landing carries it to merge.
 */
export function buildPipelineSpans(
	key: string,
	title: string,
	snapshot: StateSnapshot,
	now: number,
): Span[] {
	const run = [...snapshot.runs].reverse().find((record) => queueKey(record.repository, record.pullRequest) === key);
	const reviewEvents = snapshot.reviewEvents.filter((event) => event.key === key);
	const landingEvents = snapshot.landingEvents.filter((event) => event.pullRequestKey === key);
	const receipt = snapshot.receipts.get(key);

	const children: Span[] = [];

	if (run) {
		const status = runStateStatus(run.state);
		children.push({
			id: `${key}/run`,
			label: `run${GLYPH.breadcrumb}${run.state}`,
			detail: [run.backend && `${run.backend}/${run.model}`, run.headSha && run.headSha.slice(0, 7), run.reason]
				.filter(Boolean)
				.join(` ${GLYPH.dot} `),
			status,
			startedAt: run.createdAt || undefined,
			endedAt: status === "running" ? undefined : run.updatedAt || undefined,
		});
	}

	if (reviewEvents.length > 0) {
		const last = reviewEvents[reviewEvents.length - 1]!;
		const status = reviewEventStatus(last.state);
		const steps: Span[] = reviewEvents.map((event, index) => {
			const settled = index < reviewEvents.length - 1;
			return {
				id: `${key}/review/${index}`,
				label: event.state,
				detail: event.note || undefined,
				// A step the log has already moved past is finished, whatever it was
				// called while it ran: only the newest event may still be in flight.
				status: settled ? settledStatus(reviewEventStatus(event.state)) : reviewEventStatus(event.state),
				startedAt: event.timestamp,
				endedAt: settled ? reviewEvents[index + 1]!.timestamp : undefined,
				badge: event.state === "cached" ? "CACHED" : undefined,
			};
		});

		if (receipt) {
			steps.push({
				id: `${key}/receipt`,
				label: `receipt${GLYPH.breadcrumb}${receipt.state}`,
				detail: severitySummary(receipt.counts) || undefined,
				status: receipt.state === "complete" ? "success" : receipt.state === "findings" ? "findings" : "failure",
				children: [
					...receipt.findings.map((finding, index) => ({
						id: `${key}/finding/${index}`,
						label: `${finding.severity} ${GLYPH.dot} ${finding.file}:${finding.line}`,
						detail: finding.title,
						status: SEVERITY_STATUS[finding.severity],
					})),
					...receipt.verification.map((item, index) => ({
						id: `${key}/verification/${index}`,
						label: item.name,
						detail: item.evidence,
						status:
							item.state === "verified" ? ("success" as const) : item.state === "skipped" ? ("skipped" as const) : ("findings" as const),
					})),
				],
			});
		}

		children.push({
			id: `${key}/review`,
			label: "review",
			detail: last.note || undefined,
			status,
			startedAt: reviewEvents[0]!.timestamp,
			endedAt: status === "running" ? undefined : last.timestamp,
			children: steps,
		});
	}

	if (landingEvents.length > 0) {
		const last = landingEvents[landingEvents.length - 1]!;
		const status = last.done ? "success" : landingStateStatus(last.state ?? "");
		children.push({
			id: `${key}/landing`,
			label: "landing",
			detail: last.note || undefined,
			status,
			startedAt: landingEvents[0]!.timestamp,
			endedAt: status === "running" ? undefined : last.timestamp,
			children: landingEvents.map((event, index) => {
				const settled = index < landingEvents.length - 1;
				const own = event.done ? ("success" as SpanStatus) : landingStateStatus(event.state ?? "");
				const headDetail = event.headSha ? `head ${event.headSha.slice(0, 7)}` : "";
				const noteDetail = event.note || "";
				const detail = [headDetail, noteDetail].filter(Boolean).join(" · ") || undefined;
				return {
					id: `${key}/landing/${index}`,
					label: event.done ? "done" : (event.state ?? event.watchStatus ?? "event"),
					detail,
					status: settled ? settledStatus(own) : own,
					endedAt: settled ? landingEvents[index + 1]!.timestamp : undefined,
				};
			}),
		});
	}

	if (children.length === 0) {
		// Say so on the one visible line. A collapsed child would hide the fact that
		// nothing has run, which reads as "loading" instead of "never started".
		return [{ id: key, label: key, detail: "no recorded pipeline state", status: "pending" }];
	}

	const rollUp: SpanStatus = children.some((child) => child.status === "running")
		? "running"
		: children.some((child) => child.status === "failure")
			? "failure"
			: children.some((child) => child.status === "findings")
				? "findings"
				: "success";

	const starts = children.map((child) => child.startedAt).filter((value): value is number => value !== undefined);
	const ends = children.map((child) => child.endedAt).filter((value): value is number => value !== undefined);

	return [
		{
			id: key,
			label: key,
			detail: title,
			status: rollUp,
			startedAt: starts.length > 0 ? Math.min(...starts) : undefined,
			endedAt: rollUp === "running" || ends.length === 0 ? undefined : Math.max(...ends),
			children,
		},
	];
}
