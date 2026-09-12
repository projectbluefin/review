/**
 * The dashboard: a focusable, full-width Dagger-style view of the review queue
 * and the pipeline behind the selected item.
 *
 * Left pane is the queue, right pane is the trace. The trace pane is the same
 * renderer that draws the rail, so a step looks the same everywhere. Keys follow
 * Dagger's TUI: `j/k` move, `h/l` collapse and expand, `/` filters, `?` explains,
 * and the action keys act on whatever the cursor is on.
 *
 * The component owns no policy: it resolves with a `DashboardAction` and lets the
 * extension decide what that means.
 */

import { GLYPH, SPINNER_TICK_MS, type Painter, formatDuration, statusIcon, statusRole } from "./glyphs.ts";
import type { QueueItem } from "./github.ts";
import { type KeyMatcher, canonicalKey, rawKeyMatcher } from "./keys.ts";
import { type ReviewMode, ciGlyph } from "./mode.ts";
import { type RailKey, keymapBar, orderSourceLabel, priorityChip, tmuxReviewStatusBar } from "./rail.ts";
import { type RenderedRow, type Span, findSpan, hasChildren, renderSpanTree, visibleSpanIds } from "./trace.ts";
import { fitToWidth, truncateToWidth } from "./width.ts";
import { BLUEFIN_RAPTOR_BANNER, renderRaptorGlyph } from "./mascot.ts";
export type DashboardAction =
	| { kind: "close" }
	| { kind: "review"; item: QueueItem; items?: QueueItem[] }
	| { kind: "diff"; item: QueueItem; items?: QueueItem[] }
	| { kind: "docs"; item: QueueItem; items?: QueueItem[] }
	| { kind: "approve"; item: QueueItem; items?: QueueItem[] }
	| { kind: "fix"; item: QueueItem; items?: QueueItem[] }
	| { kind: "slay"; item: QueueItem; items?: QueueItem[] }
	| { kind: "reference"; item: QueueItem; items?: QueueItem[] }
	| { kind: "scope" }
	| { kind: "snapshot" }
	| { kind: "leaderboard" };

export const DASHBOARD_KEYS: readonly RailKey[] = [
	{ chord: "s", label: "autoslay" },
	{ chord: "r/enter", label: "review" },
	{ chord: "a", label: "approve" },
	{ chord: "space", label: "select" },
	{ chord: "A", label: "all" },
	{ chord: "x", label: "clear" },
	{ chord: "j/k", label: "move" },
	{ chord: "tab", label: "pane" },
	{ chord: "h/l", label: "fold" },
	{ chord: "i", label: "prs/issues" },
	{ chord: "H", label: "hive" },
	{ chord: "L", label: "stage" },
	{ chord: "d", label: "diff" },
	{ chord: "D", label: "docs" },
	{ chord: "f", label: "fix" },
	{ chord: "y", label: "cite" },
	{ chord: "*", label: "leaders" },
	{ chord: "o", label: "repo" },
	{ chord: "/", label: "filter" },
	{ chord: "q", label: "close" },
];

const HELP: readonly string[] = [
	"Bluefin review dashboard",
	"",
	"  space            toggle selection on the highlighted item",
	"  x                clear all selections",
	"  A                select every row the filters left on screen (max 25)",
	"  tab              switch between queue and trace",
	"  h / l, ← / →     collapse or expand a trace span",
	"  g / G            jump to first or last row",
	"  i                toggle pull requests and issues",
	"  H                toggle hive-only filter (default: on)",
	"  L                step through Hive's triage stages, then back to all",
	"  o                review another repository (owner/repo)",
	"  u                refetch the queue now",
	"  /                search/filter by title, repo, author, label, number",
	"                   (in search: type to live-filter, tab/space to select, A to select all)",
	"  enter / r        review the selected pull request",
	"  d                inspect its bounded diff",
	"  D                update documentation enforcing agentic docs system",
	"  a                verify checks, approve, squash merge",
	"  f                fix the findings reported for it",
	"  s                slay: on a pull request review, patch, verify, land;",
	"                   on an issue implement it and open a pull request",
	"  y                cite the selection in the prompt",
	"  b                build a container snapshot",
	"  *                hive weekly leaderboard (top 25 contributors)",
	"  ?                close this help",
	"  q, esc           close the dashboard",
	"",
	"Trace spans come from the appliance's own durable state under",
	"bluefin-review/: run-state, review batches, landings, receipts.",
	"Live turn spans come from this session's tool executions.",
	"",
	"Order comes from Hive when a hub is configured — its queue, its",
	"positions, never recomputed here. Without a hub the queue is",
	"categorized locally: ready, findings, blocked, waiting, deps,",
	"draft, stale. The header always names which one ran.",
];

interface TuiLike {
	requestRender(): void;
}

type Pane = "queue" | "trace";

/** Minimum width before the panes stack instead of sitting side by side. */
const SPLIT_MIN_WIDTH = 96;

export class ReviewDashboard {
	/** Set by the TUI when focus changes (`Focusable`). */
	focused = true;

	private pane: Pane = "queue";
	private traceCursor = 0;
	private queueScroll = 0;
	private traceScroll = 0;
	private frame = 0;
	private showHelp = false;
	private filtering = false;
	private filterDraft = "";
	private expansion = new Map<string, boolean>();
	private stopTick: (() => void) | undefined;

	private readonly tui: TuiLike;
	private readonly painter: Painter;
	private readonly mode: ReviewMode;
	private readonly done: (action: DashboardAction) => void;
	private readonly onRefresh: () => void;
	private readonly rows: number;
	private readonly matchKey: KeyMatcher;

	constructor(
		tui: TuiLike,
		painter: Painter,
		mode: ReviewMode,
		done: (action: DashboardAction) => void,
		onRefresh: () => void,
		rows = 22,
		matchKey: KeyMatcher = rawKeyMatcher,
	) {
		this.tui = tui;
		this.painter = painter;
		this.mode = mode;
		this.done = done;
		this.onRefresh = onRefresh;
		this.rows = rows;
		this.matchKey = matchKey;
		const handle = setInterval(() => {
			try {
				this.frame += 1;
				this.mode.refreshState();
				this.tui.requestRender();
			} catch {
				// Never let a repaint tick escape: an uncaught throw here ends the session.
			}
		}, SPINNER_TICK_MS * 2);
		(handle as { unref?(): void }).unref?.();
		this.stopTick = () => clearInterval(handle);
	}

	// ---- trace helpers -------------------------------------------------------

	private traceRoots(now: number): Span[] {
		return [...this.mode.pipelineSpans(now), ...this.mode.session.roots()];
	}

	private traceIds(now: number): string[] {
		return visibleSpanIds(this.traceRoots(now), this.expansion);
	}

	private toggleSpan(open: boolean): void {
		const ids = this.traceIds(Date.now());
		const id = ids[this.traceCursor];
		if (!id) return;
		const span = findSpan(this.traceRoots(Date.now()), id);
		if (!span || !hasChildren(span)) return;
		this.expansion.set(id, open);
	}

	// ---- input ---------------------------------------------------------------

	handleInput(data: string): void {
		const key = canonicalKey(data, this.matchKey);
		if (this.filtering) {
			this.handleFilterInput(key, data);
			return;
		}

		switch (key) {
			case "escape":
			case "q":
				this.done({ kind: "close" });
				return;
			case "?":
				this.showHelp = this.showHelp === false;
				return;
			case "tab":
				this.pane = this.pane === "queue" ? "trace" : "queue";
				return;
			case "/":
				this.filtering = true;
				this.filterDraft = this.mode.filter;
				return;
			case " ":
			case "space":
				this.toggleSelection();
				return;
			case "A":
				this.mode.selectAllVisible();
				return;
			case "x":
				this.mode.clearSelected();
				return;
			case "j":
			case "down":
				this.move(1);
				return;
			case "k":
			case "up":
				this.move(-1);
				return;
			case "g":
				this.jump(0);
				return;
			case "G":
				this.jump(Number.MAX_SAFE_INTEGER);
				return;
			case "h":
			case "left":
				if (this.pane === "trace") this.toggleSpan(false);
				else this.pane = "queue";
				return;
			case "l":
			case "right":
				if (this.pane === "trace") this.toggleSpan(true);
				else this.pane = "trace";
				return;
			case "i":
				this.mode.toggleMode();
				this.onRefresh();
				return;
			case "H":
				this.mode.toggleHiveOnly();
				return;
			case "L":
				this.mode.cycleHiveLevel();
				return;
			case "u":
				this.onRefresh();
				return;
			case "o":
				this.done({ kind: "scope" });
				return;
			case "b":
				this.done({ kind: "snapshot" });
				return;
			case "*":
				this.done({ kind: "leaderboard" });
				return;
			default:
				break;
		}

		const activeItem = this.mode.selected();
		if (!activeItem) return;
		const chosenItems = this.chosenItems();
		const items = chosenItems.length > 0 ? chosenItems : undefined;
		const item = items ? items[0]! : activeItem;
		switch (key) {
			case "r":
			case "return":
			case "enter":
				this.done({ kind: "review", item, items });
				return;
			case "d":
				this.done({ kind: "diff", item, items });
				return;
			case "D":
				this.done({ kind: "docs", item, items });
				return;
			case "a":
				this.done({ kind: "approve", item, items });
				return;
			case "f":
				this.done({ kind: "fix", item, items });
				return;
			case "s": {
				const slayable = this.mode.slayableItems();
				const batch = chosenItems.length > 0 ? chosenItems : (slayable.length > 0 ? slayable.slice(0, 7) : undefined);
				const targetItem = batch && batch.length > 0 ? batch[0]! : activeItem;
				this.done({ kind: "slay", item: targetItem, items: batch && batch.length > 1 ? batch : undefined });
				return;
			}
			case "y":
				this.done({ kind: "reference", item, items });
				return;
			default:
				break;
		}
	}

	private activeItems(): QueueItem[] {
		return this.mode.visibleItems();
	}

	private handleFilterInput(key: string, data: string): void {
		if (key === "escape") {
			this.filtering = false;
			return;
		}
		if (key === "return") {
			this.filtering = false;
			this.mode.setFilter(this.filterDraft.trim());
			this.queueScroll = 0;
			return;
		}
		if (key === "tab") {
			// Tab in search mode toggles selection on the currently highlighted item
			this.toggleSelection();
			return;
		}
		if (key === "down") {
			this.move(1);
			return;
		}
		if (key === "up") {
			this.move(-1);
			return;
		}
		if (key === "A") {
			this.mode.selectAllVisible();
			return;
		}
		if (key === "backspace") {
			this.filterDraft = this.filterDraft.slice(0, -1);
			return;
		}
		if (data === " " || key === "space") {
			this.filterDraft += " ";
			return;
		}
		if (data.length === 1 && data.charCodeAt(0) >= 32) {
			this.filterDraft += data;
		}
	}
	private move(delta: number): void {
		if (this.pane === "queue") {
			this.mode.move(delta);
			this.traceCursor = 0;
			return;
		}
		const ids = this.traceIds(Date.now());
		if (ids.length === 0) return;
		this.traceCursor = Math.min(ids.length - 1, Math.max(0, this.traceCursor + delta));
	}

	private jump(target: number): void {
		if (this.pane === "queue") {
			this.mode.cursor = Math.min(target, Math.max(0, this.mode.visibleItems().length - 1));
			this.traceCursor = 0;
			return;
		}
		this.traceCursor = Math.min(target, Math.max(0, this.traceIds(Date.now()).length - 1));
	}
	private toggleSelection(): void {
		this.mode.toggleSelected();
		this.move(1);
	}

	private chosenItems(): QueueItem[] {
		return this.mode.chosenItems();
	}

	// ---- rendering -----------------------------------------------------------

	private headerRow(width: number, now: number): string {
		const tally = this.mode.ciTally();
		const parts = [
			this.painter.bold(this.painter.fg("accent", `${GLYPH.hex} bluefin review`)),
			this.painter.fg("dim", GLYPH.logDashed.trim()),
			this.painter.bold(this.painter.fg("text", this.mode.queueMode === "prs" ? "PULL REQUESTS" : "ISSUES")),
			this.painter.fg("text", this.mode.position()),
			this.painter.fg("dim", GLYPH.dot),
			this.painter.fg("dim", this.mode.scopeLabel()),
			this.painter.fg("dim", GLYPH.dot),
			this.painter.fg("success", `${statusIcon("success")}${tally.success}`),
			this.painter.fg("error", `${statusIcon("failure")}${tally.failure}`),
			this.painter.fg("warning", `${GLYPH.running}${tally.pending}`),
			this.painter.fg("dim", GLYPH.dot),
			this.painter.fg(
				this.mode.queueError ? "error" : "dim",
				this.mode.queueError ??
					(this.mode.loading
						? `${this.painter.fg("warning", statusIcon("running", this.frame))} ${this.painter.fg("warning", "refreshing…")}`
						: `${formatDuration(Math.max(0, now - this.mode.fetchedAt))} ago`),
			),
		];
		const source = orderSourceLabel(this.mode);
		const sourceText = this.mode.hiveOnly && this.mode.hive.online ? `${source.text} (hive-only)` : source.text;
		if (this.mode.hiveLevel !== undefined) {
			parts.push(this.painter.fg("dim", GLYPH.dot), this.painter.fg("warning", `stage ${this.mode.hiveLevel}`));
		}
		parts.push(this.painter.fg("dim", GLYPH.dot), this.painter.fg(source.role, sourceText));
		for (const group of this.mode.hive.triage) {
			if (group.count > 0) parts.push(this.painter.fg("dim", `${group.label.toLowerCase()} ${group.count}`));
		}
		return truncateToWidth(parts.join(" "), width);
	}

	private queueRows(width: number, height: number): string[] {
		const items = this.activeItems();
		const rows: string[] = [];
		if (items.length === 0) {
			let emptyMsg: string;
			if (this.mode.loading) {
				emptyMsg = `  ${this.painter.fg("warning", statusIcon("running", this.frame))} loading queue…`;
			} else if (this.mode.hiveOnly && this.mode.hive.online && this.mode.items.length > 0) {
				emptyMsg = `  no Hive-ranked ${this.mode.queueMode} (${this.mode.items.length} unranked open — press H to show all, i for ${this.mode.queueMode === "prs" ? "issues" : "prs"})`;
			} else {
				emptyMsg = "  nothing open";
			}
			rows.push(this.painter.fg("dim", emptyMsg));
			return rows;
		}

		this.queueScroll = clampScroll(this.mode.cursor, this.queueScroll, height);
		let lastRepo: string | undefined;
		for (let i = this.queueScroll; i < Math.min(items.length, this.queueScroll + height); i++) {
			const item = items[i]!;
			// For issue queues or multi-repo queues, delineate transitions between repositories
			if (item.repo !== lastRepo) {
				if (lastRepo !== undefined && rows.length < height) {
					const divider = `─── ${item.repo} `.padEnd(width, "─");
					rows.push(truncateToWidth(this.painter.fg("dim", divider), width));
				}
				lastRepo = item.repo;
			}
			if (rows.length >= height) break;

			const active = i === this.mode.cursor;
			const isChecked = this.mode.selectedKeys.has(`${item.repo}#${item.id}`);
			const check = isChecked ? this.painter.fg("accent", "☒") : this.painter.fg("dim", "☐");
			const ci = ciGlyph(item.ciStatus);
			const caret = active
				? this.painter.inverse(GLYPH.caretClosed)
				: this.painter.fg("dim", GLYPH.leaf);
			const number = this.painter.fg("accent", `#${item.id}`);
			const icon = this.painter.fg(statusRole(ci.status), ci.glyph);
			const title = active
				? this.painter.bold(this.painter.fg("text", item.title))
				: this.painter.fg("text", item.title);
			const chip = priorityChip(this.painter, this.mode.priorityFor(item));
			const meta = this.painter.fg("dim", ` ${GLYPH.dot} ${shortRepo(item.repo)} @${item.author}`);
			rows.push(truncateToWidth(`${caret} ${check} ${icon} ${chip ? `${chip} ` : ""}${number} ${title}${meta}`, width));
		}

		if (items.length > this.queueScroll + height) {
			rows[rows.length - 1] = truncateToWidth(
				this.painter.fg("dim", `${GLYPH.logDashed}…${items.length - (this.queueScroll + height) + 1} more`),
				width,
			);
		}
		return rows;
	}

	/**
	 * What Hive knows about the selected row.
	 *
	 * Hive's queue is mostly issues, and an issue has no recorded pipeline, so
	 * the pane that exists to explain the selection was blank for exactly the
	 * work this tool is pointed at. This is the drill-down: why it is ranked
	 * where it is, what stage Hive has it at, and whether a change already
	 * exists that would close it.
	 */
	private hiveRows(width: number): string[] {
		const item = this.mode.selected();
		if (!item) return [];
		const work = this.mode.hiveWorkFor(item);
		const priority = this.mode.priorityFor(item);
		if (!work && priority?.hiveRank === undefined) return [];

		const rows: string[] = [];
		const rank = priority?.hiveRank === undefined ? "" : `hive #${priority.hiveRank + 1}`;
		const stage = work?.level ? ` ${GLYPH.dot} stage ${work.level}` : "";
		rows.push(truncateToWidth(`  ${this.painter.fg("accent", rank)}${this.painter.fg("dim", stage)}`, width));
		// The reason only earns a row when it says something the rank and stage
		// above it do not: `hive <level> #<n>` is the same sentence twice.
		if (priority?.reason && priority.hiveRank === undefined) {
			rows.push(truncateToWidth(this.painter.fg("dim", `  ${priority.reason}`), width));
		}
		const claimedBy = this.mode.claimFor(item);
		if (claimedBy) {
			rows.push(truncateToWidth(this.painter.fg("warning", `  a worker is on this now: ${claimedBy}`), width));
		}
		if (work && work.key !== `${item.repo}#${item.id}`) {
			// Ranked through the issue it closes, not on its own name.
			rows.push(truncateToWidth(this.painter.fg("dim", `  queued as ${work.key}`), width));
		}
		if (work?.labels.length) {
			rows.push(truncateToWidth(this.painter.fg("dim", `  labels ${work.labels.join(", ")}`), width));
		}
		if (work?.url) rows.push(truncateToWidth(this.painter.fg("dim", `  ${work.url}`), width));

		// Changes that already answer this queued work, so a maintainer never
		// starts an issue somebody has already finished. Hive's own link is
		// authoritative and survives a queue window that never fetched the pull
		// request; the fetched queue adds any others that close the same issue.
		const answering = this.mode.items.filter(
			(candidate) => candidate.type === "pr" && (candidate.closingIssues ?? []).includes(work?.key ?? ""),
		);
		if (work?.pr) {
			const state = work.pr.state ? ` ${GLYPH.dot} ${work.pr.state}` : "";
			rows.push(
				truncateToWidth(
					`  ${this.painter.fg("accent", `#${work.pr.number}`)}${this.painter.fg("dim", `${state} ${GLYPH.dot} hive`)}`,
					width,
				),
			);
		}
		for (const pr of answering) {
			if (pr.id === work?.pr?.number) continue;
			const ci = ciGlyph(pr.ciStatus);
			rows.push(
				truncateToWidth(
					`  ${this.painter.fg(statusRole(ci.status), ci.glyph)} ${this.painter.fg("accent", `#${pr.id}`)} ${this.painter.fg("text", pr.title)}`,
					width,
				),
			);
		}
		if (item.type === "issue" && item.closedByPrs && item.closedByPrs.length > 0) {
			rows.push(
				truncateToWidth(
					`  ${this.painter.fg("accent", "merged PR:")} ${this.painter.fg("warning", item.closedByPrs.join(", "))} ${this.painter.fg("dim", "(slay to verify and close)")}`,
					width,
				),
			);
		} else if (work && !work.pr && answering.length === 0) {
			rows.push(truncateToWidth(this.painter.fg("dim", "  no open change closes this yet"), width));
		}
		rows.push("");
		return rows;
	}

	private traceRows(width: number, height: number, now: number): string[] {
		const hive = this.hiveRows(width);
		const roots = this.traceRoots(now);
		if (roots.length === 0) {
			return [...hive, this.painter.fg("dim", `  ${statusIcon("pending")} no pipeline state recorded yet`)];
		}
		const ids = this.traceIds(now);
		this.traceCursor = Math.min(this.traceCursor, Math.max(0, ids.length - 1));
		const focusedId = this.pane === "trace" ? ids[this.traceCursor] : undefined;

		const rendered: RenderedRow[] = renderSpanTree(roots, {
			painter: this.painter,
			width,
			now,
			frame: this.frame,
			expansion: this.expansion,
			focusedId,
			maxLogLines: Math.max(3, Math.floor((height - hive.length) / 3)),
		});

		if (focusedId === undefined) {
			// Nothing is under the cursor: follow the tail while work is in flight,
			// the way a build log does, so the running step stays on screen.
			const live = roots.some((span) => span.status === "running");
			this.traceScroll = live ? Math.max(0, rendered.length - height) : 0;
		} else {
			const cursorRow = rendered.findIndex((row) => row.kind === "span" && row.spanId === focusedId);
			this.traceScroll = clampScroll(Math.max(0, cursorRow), this.traceScroll, height);
		}
		const spans = rendered
			.slice(this.traceScroll, this.traceScroll + Math.max(1, height - hive.length))
			.map((row) => row.text);
		return [...hive, ...spans];
	}

	private paneTitle(pane: Pane, text: string, width: number): string {
		const focused = this.pane === pane;
		const marker = focused ? this.painter.fg("accent", GLYPH.caretOpen) : this.painter.fg("dim", GLYPH.caretClosed);
		const label = focused ? this.painter.bold(this.painter.fg("text", text)) : this.painter.fg("dim", text);
		return truncateToWidth(`${marker} ${label}`, width);
	}

	render(width: number): string[] {
		const now = Date.now();
		const lines: string[] = [this.headerRow(width, now), this.painter.fg("border", "─".repeat(width))];

		if (this.showHelp) {
			for (const bannerLine of BLUEFIN_RAPTOR_BANNER) {
				lines.push(truncateToWidth(this.painter.fg("accent", bannerLine), width));
			}
			lines.push("");
			for (const line of HELP) lines.push(truncateToWidth(this.painter.fg(line.startsWith("  ") ? "dim" : "text", line), width));
			lines.push(keymapBar(this.painter, [{ chord: "?", label: "back" }], width));
			return lines;
		}
		const bodyHeight = Math.max(4, this.rows - 4);
		const item = this.mode.selected();
		const traceTitle = item ? `TRACE ${item.repo}#${item.id}` : "TRACE";

		if (width >= SPLIT_MIN_WIDTH) {
			const leftWidth = Math.floor((width - 3) / 2);
			const rightWidth = width - leftWidth - 3;
			const queueTitle = this.mode.selectedKeys.size > 0
				? `QUEUE ${this.mode.position()} (${this.mode.selectedKeys.size} selected)`
				: `QUEUE ${this.mode.position()}`;
			const left = [this.paneTitle("queue", queueTitle, leftWidth), ...this.queueRows(leftWidth, bodyHeight - 1)];
			const right = [this.paneTitle("trace", traceTitle, rightWidth), ...this.traceRows(rightWidth, bodyHeight - 1, now)];
			const separator = this.painter.fg("border", GLYPH.railBar.trim());

			for (let i = 0; i < bodyHeight; i++) {
				const leftCell = fitToWidth(left[i] ?? "", leftWidth);
				const rightCell = right[i] ?? "";
				lines.push(truncateToWidth(`${leftCell} ${separator} ${rightCell}`, width));
			}
		} else {
			const queueHeight = Math.max(3, Math.floor((bodyHeight - 2) / 2));
			const queueTitle = this.mode.selectedKeys.size > 0
				? `QUEUE ${this.mode.position()} (${this.mode.selectedKeys.size} selected)`
				: `QUEUE ${this.mode.position()}`;
			lines.push(this.paneTitle("queue", queueTitle, width));
			lines.push(...this.queueRows(width, queueHeight));
			lines.push(this.paneTitle("trace", traceTitle, width));
			lines.push(...this.traceRows(width, bodyHeight - queueHeight - 2, now));
		}
		if (this.filtering) {
			const searchPrompt = `${this.painter.fg("accent", "search")} ${this.painter.fg("text", this.filterDraft)}${this.painter.inverse(" ")}`;
			const hints = this.painter.fg("dim", "  (tab/space: select · ↑/↓: navigate · A: select all · enter: done · esc: cancel)");
			lines.push(truncateToWidth(`${searchPrompt}${hints}`, width));
		} else if (this.mode.filter) {
			lines.push(truncateToWidth(this.painter.fg("dim", `filter: ${this.mode.filter}  (/ to search · esc/clear to reset)`), width));
		}
		lines.push(keymapBar(this.painter, DASHBOARD_KEYS, width));
		lines.push(tmuxReviewStatusBar(this.mode, this.painter, width, now));
		return lines;
	}

	invalidate(): void {}

	dispose(): void {
		this.stopTick?.();
		this.stopTick = undefined;
	}
}

/** Keep `cursor` inside a `height`-row window without jumping the viewport. */
function clampScroll(cursor: number, scroll: number, height: number): number {
	if (height <= 0) return 0;
	if (cursor < scroll) return cursor;
	if (cursor >= scroll + height) return cursor - height + 1;
	return scroll;
}

function shortRepo(repo: string): string {
	const slash = repo.indexOf("/");
	return slash < 0 ? repo : repo.slice(slash + 1);
}
