/**
 * Span model and Dagger-style tree renderer.
 *
 * A span is one step of work with a status and a clock. Everything the dashboard
 * shows — a review pipeline reconstructed from durable state, a live omp turn, the
 * findings inside a receipt — is projected into this shape and drawn by one
 * renderer, so a Bluefin trace and an agent turn read identically.
 *
 * Rendering is pure: it takes spans, a `Painter`, and a width, and returns rows.
 * No TUI, no clock, no filesystem. The dashboard owns cursor, expansion, and time.
 */

import {
	GLYPH,
	PLAIN_PAINTER,
	type Painter,
	type SpanStatus,
	formatDuration,
	restrainedRole,
	statusIcon,
	statusRole,
} from "./glyphs.ts";
import { truncateToWidth } from "./width.ts";

/**
 * Why a span is red, if it is. The failure taxonomy of issue #465.
 *
 * The visual `SpanStatus` only says "failed" or "not"; this says *what* failed
 * so the five classes the invariant names never read the same: a real PR check,
 * an unavailable verification, a review-environment failure, an agent/tool
 * failure, and a cancellation. Non-failure spans carry their own value too
 * (`success`, `findings`, `running`, `pending`), so the taxonomy is the single
 * reason a span carries rather than a second boolean next to `status`.
 */
export type TraceClass =
	| "pending"
	| "running"
	| "success"
	| "cached"
	| "findings"
	/** Work was cancelled on purpose — normal, not a failure. */
	| "cancelled"
	/** A GitHub check on the pull request failed — the one a maintainer acts on. */
	| "pr-check"
	/** Verification evidence is unavailable or incomplete (workspace mismatch, #471). */
	| "verification"
	/** The review/workspace environment failed, not the PR's own checks. */
	| "environment"
	/** An agent tool execution failed. */
	| "tool";

/**
 * The visible right-hand badge for a class. Empty for the non-failure classes,
 * which need no label: a green check and a cancelled span already say what they
 * mean. A failure class always renders its tag so the taxonomy is seen, not
 * inferred from a shared red `✘`.
 */
export const TRACE_CLASS_LABEL: Record<TraceClass, string> = {
	pending: "",
	running: "",
	success: "",
	cached: "CACHED",
	findings: "",
	cancelled: "CANCELLED",
	"pr-check": "CHECK",
	verification: "UNVERIFIED",
	environment: "WORKSPACE",
	tool: "TOOL",
};

/** Badge text for a span's class, or "" when the class needs no label. */
export function traceClassBadge(cls?: TraceClass): string {
	return cls ? TRACE_CLASS_LABEL[cls] : "";
}

/**
 * The visual `SpanStatus` a class renders as. Every failure class collapses to
 * `failure` (a red `✘` either way) and `cancelled` to `skipped`; the taxonomy
 * lives in `cls`, not in a fifth shade of red. This keeps the icon vocabulary
 * stable while the reason a span failed becomes distinct.
 */
export function classStatus(cls: TraceClass): SpanStatus {
	switch (cls) {
		case "cancelled":
			return "skipped";
		case "pr-check":
		case "verification":
		case "environment":
		case "tool":
			return "failure";
		default:
			return cls;
	}
}

export interface Span {
	/** Stable across refreshes: cursor position and expansion survive a re-poll. */
	id: string;
	label: string;
	/** Dim trailing context: author, reason, counts. */
	detail?: string;
	status: SpanStatus;
	/**
	 * Why a span is red, if it is. The failure taxonomy of issue #465: the visual
	 * `status` only says "failed", this says *what* failed so a real PR check,
	 * an unavailable verification, a review-environment failure, an agent/tool
	 * failure, and a cancellation never read the same. Rendered as a badge.
	 */
	cls?: TraceClass;
	startedAt?: number;
	endedAt?: number;
	/** Right-hand badge, e.g. `CACHED` or `ERROR`. Folded with the class label. */
	badge?: string;
	/** Streamed output; tailed under the span while it is expanded. */
	logs?: string[];
	children?: Span[];
}

export interface RenderedRow {
	spanId: string;
	depth: number;
	kind: "span" | "log";
	text: string;
}

export interface TreeOptions {
	painter: Painter;
	width: number;
	/** Wall clock used for live durations, injected so tests are deterministic. */
	now: number;
	/** Spinner frame counter. */
	frame?: number;
	/** Explicit expand/collapse decisions; absent ids fall back to `defaultExpanded`. */
	expansion?: ReadonlyMap<string, boolean>;
	/** Span id under the cursor. */
	focusedId?: string;
	/** Log lines kept per expanded span (Dagger caps inline logs at a third of the screen). */
	maxLogLines?: number;
}

/** Elapsed time, or undefined for a step that never started. */
export function spanDuration(span: Span, now: number): number | undefined {
	if (span.startedAt === undefined) return undefined;
	const end = span.endedAt ?? now;
	return Math.max(0, end - span.startedAt);
}

export function hasChildren(span: Span): boolean {
	return (span.children?.length ?? 0) > 0;
}

/**
 * Dagger's default: work in flight or needing attention opens itself; anything
 * settled and uninteresting stays one line.
 */
export function defaultExpanded(span: Span): boolean {
	return span.status === "running" || span.status === "failure" || span.status === "findings";
}

function isExpanded(span: Span, expansion?: ReadonlyMap<string, boolean>): boolean {
	const explicit = expansion?.get(span.id);
	return explicit ?? defaultExpanded(span);
}

/** Deepest running descendant, used for the one-line status rail. */
export function activeSpan(span: Span): Span | undefined {
	if (span.status !== "running") return undefined;
	for (const child of span.children ?? []) {
		const active = activeSpan(child);
		if (active) return active;
	}
	return span;
}

function renderSpanRow(span: Span, prefix: string, options: TreeOptions): string {
	const { painter, now, frame = 0 } = options;
	const expanded = isExpanded(span, options.expansion);
	const focused = options.focusedId === span.id;
	const role = statusRole(span.status);

	const caretGlyph = hasChildren(span) ? (expanded ? GLYPH.caretOpen : GLYPH.caretClosed) : GLYPH.leaf;
	const caret = focused ? painter.inverse(caretGlyph) : painter.fg(hasChildren(span) ? role : "dim", caretGlyph);
	const icon = painter.fg(role, statusIcon(span.status, frame));

	let line = `${prefix}${caret} ${icon} `;
	line += span.status === "pending" ? painter.fg("dim", span.label) : painter.bold(painter.fg("text", span.label));

	const elapsed = spanDuration(span, now);
	if (elapsed !== undefined) {
		const text = ` ${formatDuration(elapsed)}`;
		line += painter.fg(span.status === "running" ? "warning" : "dim", text);
	}
	// The class label is the failure taxonomy made visible: a span that is merely
	// "failed" still says *why*, so a workspace mismatch never masquerades as a
	// PR check failure. An explicit badge (e.g. `CACHED`) wins when set.
	const badge = span.badge ?? traceClassBadge(span.cls);
	if (badge) line += painter.fg(role, ` ${badge}`);
	if (span.detail) line += painter.fg("dim", ` ${GLYPH.dot} ${span.detail}`);

	return truncateToWidth(line, options.width);
}

function renderLogRows(span: Span, prefix: string, options: TreeOptions): RenderedRow[] {
	const logs = span.logs ?? [];
	if (logs.length === 0) return [];

	const { painter } = options;
	const limit = Math.max(1, options.maxLogLines ?? 8);
	const gutterRole = restrainedRole(span.status);
	const rows: RenderedRow[] = [];
	const hidden = logs.length - limit;

	if (hidden > 0) {
		const bar = painter.fg(gutterRole, GLYPH.logDashed);
		rows.push({
			spanId: span.id,
			depth: 0,
			kind: "log",
			text: truncateToWidth(`${prefix}${bar}${painter.fg("dim", `…${hidden} lines hidden…`)}`, options.width),
		});
	}

	for (const log of logs.slice(Math.max(0, hidden))) {
		const bar = painter.fg(gutterRole, GLYPH.logBar);
		rows.push({
			spanId: span.id,
			depth: 0,
			kind: "log",
			text: truncateToWidth(`${prefix}${bar}${painter.fg("toolTitle", log)}`, options.width),
		});
	}
	return rows;
}

/** One ancestor's contribution to a row's left chrome. */
interface Rail {
	last: boolean;
	status: SpanStatus;
}

/**
 * Dagger's `fancyIndent`: an ancestor that still has siblings below it keeps a
 * vertical rail; the last child's rail ends and its descendants indent into blank.
 * The ancestor at depth `d` owns column `d - 1`; a root owns no column.
 */
function railPrefix(painter: Painter, rails: readonly Rail[]): string {
	let prefix = "";
	for (const rail of rails) {
		prefix += painter.fg(restrainedRole(rail.status), rail.last ? GLYPH.railGap : GLYPH.railBar);
	}
	return prefix;
}

/** Flatten spans into styled rows, honoring expansion and the tree chrome. */
export function renderSpanTree(roots: readonly Span[], options: TreeOptions): RenderedRow[] {
	const rows: RenderedRow[] = [];
	const { painter } = options;

	const walk = (span: Span, rails: Rail[], parent: Span | undefined, isLast: boolean, depth: number) => {
		const ancestorRail = railPrefix(painter, rails);
		const connector =
			parent === undefined
				? ""
				: painter.fg(restrainedRole(parent.status), isLast ? GLYPH.branchEnd : GLYPH.branchMid);

		rows.push({ spanId: span.id, depth, kind: "span", text: renderSpanRow(span, ancestorRail + connector, options) });

		if (!isExpanded(span, options.expansion)) return;

		// Children sit one column further in; this span's own rail continues only
		// while it has siblings left below it.
		const childRails = parent === undefined ? rails : [...rails, { last: isLast, status: span.status }];
		rows.push(...renderLogRows(span, railPrefix(painter, childRails), options));

		const children = span.children ?? [];
		for (let i = 0; i < children.length; i++) {
			walk(children[i]!, childRails, span, i === children.length - 1, depth + 1);
		}
	};

	for (let i = 0; i < roots.length; i++) {
		walk(roots[i]!, [], undefined, i === roots.length - 1, 0);
	}
	return rows;
}

/** Depth-first list of span ids in render order, for cursor movement. */
export function visibleSpanIds(roots: readonly Span[], expansion?: ReadonlyMap<string, boolean>): string[] {
	const ids: string[] = [];
	const walk = (span: Span) => {
		ids.push(span.id);
		if (!isExpanded(span, expansion)) return;
		for (const child of span.children ?? []) walk(child);
	};
	for (const span of roots) walk(span);
	return ids;
}

/** Locate a span by id across a forest. */
export function findSpan(roots: readonly Span[], id: string): Span | undefined {
	for (const span of roots) {
		if (span.id === id) return span;
		const hit = findSpan(span.children ?? [], id);
		if (hit) return hit;
	}
	return undefined;
}

/** Plain-text rendering for tool results and non-TTY output. */
export function traceToText(roots: readonly Span[], now: number, width = 100): string {
	return renderSpanTree(roots, { painter: PLAIN_PAINTER, width, now })
		.map((row) => row.text.trimEnd())
		.join("\n");
}
