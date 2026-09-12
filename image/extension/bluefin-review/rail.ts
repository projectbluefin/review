/**
 * The always-on rail under the editor.
 *
 * Dagger's status line and keymap bar, sized for four rows: what the queue is,
 * what is selected, what is moving right now, and which keys act on it. It ticks
 * the spinner only while something is actually running, so an idle session costs
 * no repaints.
 */

import { GLYPH, SPINNER_TICK_MS, type PaintRole, type Painter, formatDuration, statusIcon, statusRole } from "./glyphs.ts";
import { type ReviewMode, ciGlyph } from "./mode.ts";
import type { Priority, PriorityCategory } from "./priority.ts";
import { activeSpan, spanDuration } from "./trace.ts";
import { truncateToWidth, visibleWidth } from "./width.ts";
export interface RailKey {
	chord: string;
	label: string;
}

/** Dagger's keymap bar: dim, middot-separated, no boxes. */
export function keymapBar(painter: Painter, keys: readonly RailKey[], width: number): string {
	const parts = keys.map((key) => `${painter.fg("accent", key.chord)} ${painter.fg("dim", key.label)}`);
	const separator = painter.fg("dim", ` ${GLYPH.dot} `);
	return truncateToWidth(`${painter.fg("dim", GLYPH.branchEnd)} ${parts.join(separator)}`, width);
}

/**
 * Retro tmux status bar styled like the contribute runtime's lower-third bar:
 * [🦖 BLUEFIN] [review] [🐝 HIVE/LOCAL] | Task: #123 (repo) | Issues: X | PRs: Y | Workers: Z/ZZ | Reviewers: X/XX | HH:MM
 */
export function tmuxReviewStatusBar(mode: ReviewMode, painter: Painter, width: number, now: number): string {
	// Colors matching image/contribute/entrypoint.sh & image/tmux.conf:
	// left: #[bg=#1d4ed8,fg=#ffffff,bold] 🦖 BLUEFIN #[bg=#2563eb,fg=#ffffff,nobold] review #[bg=#1e40af,fg=#bfdbfe] 🐝 ${mode} #[default]
	// status-style: bg=#1e293b,fg=#93c5fd
	const hive = mode.hive;
	const hiveMode = hive.online ? "HIVE" : (hive.configured ? "OFFLINE" : "LOCAL");

	const blueBg = "\x1b[48;2;30;41;59m"; // #1e293b
	const bluefinBadge = "\x1b[48;2;29;78;216m\x1b[38;2;255;255;255m\x1b[1m 🦖 BLUEFIN \x1b[0m";
	const reviewBadge = "\x1b[48;2;37;99;235m\x1b[38;2;255;255;255m review \x1b[0m";
	const hiveBadge = `\x1b[48;2;30;64;175m\x1b[38;2;191;219;254m 🐝 ${hiveMode} \x1b[0m`;

	const item = mode.selected();
	let activeTaskStr = "";
	if (item) {
		const kind = item.type === "pr" ? "PR" : "ISSUE";
		const repo = item.repo.includes("/") ? item.repo.split("/")[1] : item.repo;
		activeTaskStr = `${blueBg}\x1b[38;2;96;165;250mTask: \x1b[1m\x1b[38;2;255;255;255m${kind} #${item.id}\x1b[0m${blueBg}\x1b[38;2;147;197;253m (${repo}) \x1b[38;2;59;130;246m| `;
	}

	const tally = mode.ciTally();
	const issuesCount = mode.queueMode === "issues" ? mode.visibleItems().length : (hive.actionableItems ?? "-");
	const prsCount = mode.queueMode === "prs" ? mode.visibleItems().length : tally.success + tally.failure + tally.pending;

	let stats = `${blueBg}\x1b[38;2;147;197;253mIssues: \x1b[1m\x1b[38;2;255;255;255m${issuesCount}\x1b[0m${blueBg}\x1b[38;2;147;197;253m \x1b[38;2;59;130;246m| \x1b[38;2;147;197;253mPRs: \x1b[1m\x1b[38;2;255;255;255m${prsCount}\x1b[0m${blueBg}\x1b[38;2;147;197;253m`;
	if (hive.workers) {
		stats += ` \x1b[38;2;59;130;246m| \x1b[38;2;147;197;253mWorkers: \x1b[1m\x1b[38;2;255;255;255m${hive.workers}\x1b[0m${blueBg}\x1b[38;2;147;197;253m`;
	}
	if (hive.reviewers) {
		stats += ` \x1b[38;2;59;130;246m| \x1b[38;2;147;197;253mReviewers: \x1b[1m\x1b[38;2;255;255;255m${hive.reviewers}\x1b[0m${blueBg}\x1b[38;2;147;197;253m`;
	}
	const date = new Date(now);
	const hours = String(date.getHours()).padStart(2, "0");
	const minutes = String(date.getMinutes()).padStart(2, "0");
	const timeStr = `\x1b[38;2;59;130;246m| \x1b[38;2;191;219;254m${hours}:${minutes}\x1b[0m`;

	const content = `${bluefinBadge}${reviewBadge}${hiveBadge}${blueBg} ${activeTaskStr}${stats} ${timeStr}\x1b[0m`;
	const contentWidth = visibleWidth(content);
	const fillSpaces = Math.max(0, width - contentWidth);
	const bar = `${bluefinBadge}${reviewBadge}${hiveBadge}${blueBg} ${activeTaskStr}${stats} ${timeStr}${" ".repeat(fillSpaces)}\x1b[0m`;
	return truncateToWidth(bar, width);
}

/**
 * Age of the queue data, shown only once it is old enough to matter.
 *
 * A live "3.1s ago" counter forces a repaint every tick and tells you nothing you
 * did not already assume. Silence means fresh; text means stale.
 */
export const STALE_AFTER_MS = 90_000;

export function queueAge(fetchedAt: number, now: number): string | undefined {
	if (!fetchedAt) return "never fetched";
	const age = Math.max(0, now - fetchedAt);
	return age < STALE_AFTER_MS ? undefined : `stale ${formatDuration(age)}`;
}


/** Colour per category: what the eye should land on first is loudest. */
const CATEGORY_ROLE: Record<PriorityCategory, PaintRole> = {
	hive: "accent",
	"ready-for-human-merge": "success",
	review: "warning",
	"resolve-conflicts": "error",
	"fix-ci": "error",
	investigate: "dim",
	triage: "muted",
};

/** Short forms, because a queue row is not a place for a sentence. */
const CATEGORY_LABEL: Record<PriorityCategory, string> = {
	hive: "hive",
	"ready-for-human-merge": "merge",
	review: "review",
	"resolve-conflicts": "conflict",
	"fix-ci": "fix-ci",
	investigate: "look",
	triage: "triage",
};

export function priorityChip(painter: Painter, priority: Priority | undefined): string {
	if (!priority) return "";
	const label =
		priority.hiveRank === undefined ? CATEGORY_LABEL[priority.category] : `hive#${priority.hiveRank + 1}`;
	return painter.fg(CATEGORY_ROLE[priority.category], label);
}

/**
 * Where the order came from.
 *
 * "Hive says nothing is urgent" and "we could not ask Hive" are different
 * facts, and a maintainer acting on the wrong one wastes a morning.
 */
export function orderSourceLabel(mode: ReviewMode): { text: string; role: PaintRole } {
	const hive = mode.hive;
	if (!hive.configured) return { text: "local ranking", role: "dim" };
	if (hive.error) return { text: `hive unreachable (${hive.error.split(" \u2192 ")[0]})`, role: "error" };
	const coverage = mode.hiveCoverage();
	if (mode.orderSource() === "hive") {
		const actionable = hive.actionableItems === undefined ? "" : ` \u00b7 ${hive.actionableItems} actionable`;
		// present/total, not a bare count: a queue missing half of Hive's work
		// looks identical to a short queue unless it says so.
		const queued = `${coverage.present}/${coverage.total} queued`;
		return { text: `hive \u25b8 ${queued}${actionable}`, role: "accent" };
	}
	if (coverage.total > 0) {
		return { text: `hive \u25b8 0/${coverage.total} queued \u00b7 none reachable here`, role: "error" };
	}
	return { text: "hive \u25b8 nothing queued here", role: "dim" };
}

/** Build the rail's rows. Pure, so the widget test needs no terminal. */
export function renderRail(
	mode: ReviewMode,
	painter: Painter,
	width: number,
	now: number,
	frame: number,
	keys: readonly RailKey[],
	options: { compact?: boolean } = { compact: true },
): string[] {
	// Option C: 1-Line Adaptive Rail
	// When running: morphs into live progress trace: ⣾ running <tool/stage> · #id · elapsed
	// When idle: #id Title (@author)  [chip]  ✔  |  alt+b: dash (pos)
	if (options.compact !== false) {
		const item = mode.selected();
		if (!item) {
			const spinner = painter.fg("warning", statusIcon("running", frame));
			let reasonText = "queue empty";
			if (mode.loading) {
				reasonText = "loading queue…";
			} else if (mode.hiveOnly && mode.hive.online && mode.items.length > 0) {
				reasonText = `no Hive-ranked ${mode.queueMode} (${mode.items.length} unranked open — H shows all)`;
			}
			const reason = mode.queueError
				? painter.fg("error", `${statusIcon("failure")} ${mode.queueError}`)
				: mode.loading
					? `${spinner} ${painter.fg("warning", reasonText)}`
					: painter.fg("dim", `${statusIcon("pending")} ${reasonText}`);
			const hint = painter.fg("dim", "alt+b: dash");
			return [truncateToWidth(`${painter.fg("accent", `${GLYPH.hex} bluefin`)} ${reason}  │  ${hint}`, width)];
		}
		const ci = ciGlyph(item.ciStatus);
		const priority = mode.priorityFor(item);
		const chip = priorityChip(painter, priority);
		const isChecked = mode.selectedKeys.has(`${item.repo}#${item.id}`);
		const check = isChecked ? painter.fg("accent", "☒ ") : "";
		const icon = painter.fg(statusRole(ci.status), ci.glyph);
		const number = painter.fg("accent", `#${item.id}`);
		const title = painter.bold(painter.fg("text", item.title));
		const author = painter.fg("dim", `@${item.author}`);
		const pos = painter.fg("dim", `(${mode.position()})`);
		const shortcut = painter.fg("dim", "alt+b: dash");
		const spinner = painter.fg("warning", statusIcon("running", frame));
		const age = mode.loading ? `${spinner} ${painter.fg("warning", "refreshing…")}` : queueAge(mode.fetchedAt, now);
		const ageBadge = age ? (mode.loading ? age : painter.fg("warning", age)) : "";

		const live = liveLine(mode, painter, now, frame);
		if (live && mode.session.active()) {
			return [truncateToWidth(live, width)];
		}

		const leftParts = [check, number, title, author, chip ? `${chip}` : "", icon, ageBadge].filter(Boolean);
		const line = `${leftParts.join(" ")}  │  ${shortcut} ${pos}`;
		return [truncateToWidth(line, width)];
	}

	const rows: string[] = [];
	const tally = mode.ciTally();
	const selCount = mode.selectedKeys.size;
	const selBadge = selCount > 0 ? ` ${GLYPH.dot} ${painter.fg("accent", `${selCount} selected`)}` : "";

	const headline = [
		painter.fg("accent", `${GLYPH.hex} bluefin`),
		painter.fg("dim", GLYPH.logDashed.trim()),
		painter.bold(painter.fg("text", mode.queueMode === "prs" ? "PRS" : "ISSUES")),
		painter.fg("text", mode.position()),
		painter.fg("dim", GLYPH.dot),
		painter.fg("dim", mode.scopeLabel()),
		painter.fg("dim", GLYPH.dot),
		painter.fg("success", `${statusIcon("success")}${tally.success}`),
		painter.fg("error", `${statusIcon("failure")}${tally.failure}`),
		painter.fg("warning", `${GLYPH.running}${tally.pending}`),
	];
	const source = orderSourceLabel(mode);
	headline.push(painter.fg("dim", GLYPH.dot), painter.fg(source.role, source.text));
	const age = mode.loading ? "refreshing" : queueAge(mode.fetchedAt, now);
	if (age) headline.push(painter.fg("dim", GLYPH.dot), painter.fg(mode.loading ? "dim" : "warning", age));
	rows.push(truncateToWidth(headline.join(" ") + selBadge, width));

	const item = mode.selected();
	if (!item) {
		let reasonText = "queue empty";
		if (mode.loading) {
			reasonText = "loading queue…";
		} else if (mode.hiveOnly && mode.hive.online && mode.items.length > 0) {
			reasonText = `no Hive-ranked ${mode.queueMode} (${mode.items.length} unranked open — H shows all, alt+i toggles prs/issues)`;
		}
		const reason = mode.queueError
			? painter.fg("error", `${statusIcon("failure")} ${mode.queueError}`)
			: painter.fg("dim", `${statusIcon("pending")} ${reasonText}`);
		rows.push(truncateToWidth(`${painter.fg("dim", GLYPH.railBar)}${reason}`, width));
	} else {
		const ci = ciGlyph(item.ciStatus);
		const priority = mode.priorityFor(item);
		const chip = priorityChip(painter, priority);
		const isChecked = mode.selectedKeys.has(`${item.repo}#${item.id}`);
		const check = isChecked ? painter.fg("accent", "☒") : painter.fg("dim", "☐");
		const icon = painter.fg(statusRole(ci.status), ci.glyph);
		const parts = [
			painter.fg("dim", GLYPH.branchMid),
			check,
			icon,
			chip,
			painter.fg("accent", `${item.repo}#${item.id}`),
			painter.fg("dim", GLYPH.dot),
			painter.bold(painter.fg("text", item.title)),
			painter.fg("dim", `@${item.author}`),
		].filter(Boolean);
		if (item.additions !== undefined && item.deletions !== undefined) {
			parts.push(painter.fg("dim", `+${item.additions} -${item.deletions}`));
		}
		if (item.draft) parts.push(painter.fg("dim", "draft"));
		rows.push(truncateToWidth(parts.join(" "), width));
	}

	const live = liveLine(mode, painter, now, frame);
	if (live) rows.push(truncateToWidth(live, width));

	rows.push(keymapBar(painter, keys, width));
	rows.push(tmuxReviewStatusBar(mode, painter, width, now));
	return rows;
}
/**
 * Render a compact hitlist view of queue items above the text input.
 * Shows up to maxItems around the currently selected cursor.
 */
export function renderHitlist(
	mode: ReviewMode,
	painter: Painter,
	width: number,
	maxItems = 5,
): string[] {
	const items = mode.visibleItems();
	const rows: string[] = [];
	const title = mode.queueMode === "issues" ? "ISSUES HITLIST" : "PR HITLIST";
	const selCount = mode.selectedKeys.size;
	const countLabel = selCount > 0 ? ` (${selCount} selected)` : "";
	const headline = [
		painter.fg("accent", `${GLYPH.hex} ${title}${countLabel}`),
		painter.fg("text", mode.position()),
		painter.fg("dim", GLYPH.dot),
		painter.fg("dim", mode.scopeLabel()),
		painter.fg("dim", GLYPH.dot),
		painter.fg("dim", "alt+j/k: browse · alt+x: select · alt+y: cite · alt+b: dash"),
	];
	rows.push(truncateToWidth(headline.join(" "), width));
	if (items.length === 0) {
		const reason = mode.queueError
			? painter.fg("error", `${statusIcon("failure")} ${mode.queueError}`)
			: painter.fg("dim", `${statusIcon("pending")} ${mode.loading ? "loading queue…" : "no items open"}`);
		rows.push(truncateToWidth(`  ${reason}`, width));
		return rows;
	}

	const total = items.length;
	const cursor = mode.cursor;
	const half = Math.floor(maxItems / 2);
	let start = Math.max(0, cursor - half);
	let end = Math.min(total, start + maxItems);
	if (end - start < maxItems && start > 0) {
		start = Math.max(0, end - maxItems);
	}

	let lastRepo: string | undefined;
	for (let i = start; i < end; i++) {
		const item = items[i]!;
		if (item.repo !== lastRepo) {
			if (lastRepo !== undefined && rows.length < maxItems + 2) {
				const divider = `─── ${item.repo} `.padEnd(width, "─");
				rows.push(truncateToWidth(painter.fg("dim", divider), width));
			}
			lastRepo = item.repo;
		}
		const active = i === cursor;
		const isChecked = mode.selectedKeys.has(`${item.repo}#${item.id}`);
		const check = isChecked ? painter.fg("accent", "☒") : painter.fg("dim", "☐");
		const ci = ciGlyph(item.ciStatus);
		const icon = mode.queueMode === "issues"
			? painter.fg("dim", GLYPH.leaf)
			: painter.fg(statusRole(ci.status), ci.glyph);
		const caret = active
			? painter.inverse(GLYPH.caretClosed)
			: painter.fg("dim", GLYPH.leaf);
		const number = painter.fg("accent", `#${item.id}`);
		const chip = priorityChip(painter, mode.priorityFor(item));
		const titleText = active
			? painter.bold(painter.fg("text", item.title))
			: painter.fg("text", item.title);
		const meta = painter.fg("dim", ` @${item.author}`);
		// Column alignment: Caret -> Check -> CI Icon -> Priority Chip -> #ID -> Title -> @Author
		const row = `${caret} ${check} ${icon} ${chip ? `${chip} ` : ""}${number} ${titleText}${meta}`;
		rows.push(truncateToWidth(row, width));
	}
	return rows;
}

/** Widget component for displaying the hitlist above the editor. */
export class ReviewHitlist {
	private cache: string[] = [];
	private cacheKey = "";

	private readonly tui: TuiLike | undefined;
	private readonly painter: Painter;
	private readonly mode: ReviewMode;
	private readonly maxItems: number;

	constructor(tui: TuiLike | undefined, painter: Painter, mode: ReviewMode, maxItems = 5) {
		this.tui = tui;
		this.painter = painter;
		this.mode = mode;
		this.maxItems = maxItems;
	}

	render(width: number): string[] {
		const rows = renderHitlist(this.mode, this.painter, width, this.maxItems);
		const key = `${width}:${this.mode.cursor}:${this.mode.queueMode}:${this.mode.selectedKeys.size}:${rows.join("\u0000")}`;
		if (key === this.cacheKey) return this.cache;
		this.cacheKey = key;
		this.cache = rows;
		return rows;
	}

	invalidate(): void {
		this.cacheKey = "";
	}

	dispose(): void {}
}

/** The one line that answers "what is happening right now". */
function liveLine(mode: ReviewMode, painter: Painter, now: number, frame: number): string | undefined {
	const turn = mode.session.active();
	if (turn) {
		const elapsed = spanDuration(turn, now);
		return [
			painter.fg("warning", GLYPH.logBar.trim()),
			painter.fg("warning", statusIcon("running", frame)),
			painter.fg("text", turn.label),
			elapsed === undefined ? "" : painter.fg("warning", formatDuration(elapsed)),
		]
			.filter(Boolean)
			.join(" ");
	}

	const [root] = mode.pipelineSpans(now);
	if (!root?.children?.length) return undefined;

	const stages = root.children.map((child) => {
		const icon = painter.fg(statusRole(child.status), statusIcon(child.status, frame));
		const elapsed = spanDuration(child, now);
		const suffix = elapsed === undefined ? "" : painter.fg("dim", ` ${formatDuration(elapsed)}`);
		return `${icon} ${painter.fg("text", child.label)}${suffix}`;
	});
	const gutterRole = root.status === "running" ? "warning" : "dim";
	const running = activeSpan(root);
	const tail = running && running !== root ? painter.fg("dim", ` ${GLYPH.breadcrumb.trim()} ${running.label}`) : "";
	return painter.fg(gutterRole, GLYPH.logBar.trim()) + " " + stages.join(painter.fg("dim", `  ${GLYPH.dot}  `)) + tail;
}

interface TuiLike {
	requestRender(): void;
}

/**
 * Widget component handed to `ctx.ui.setWidget`.
 *
 * Owns exactly one timer, started only while work is in flight, and stops it as
 * soon as nothing is running: an idle review session must not repaint at 12.5 Hz.
 */
export class ReviewRail {
	private frame = 0;
	private stopTick: (() => void) | undefined;
	private cache: string[] = [];
	private cacheKey = "";

	private readonly tui: TuiLike;
	private readonly painter: Painter;
	private readonly mode: ReviewMode;
	private readonly keys: readonly RailKey[];
	private readonly isHidden?: () => boolean;

	constructor(tui: TuiLike, painter: Painter, mode: ReviewMode, keys: readonly RailKey[], isHidden?: () => boolean) {
		this.tui = tui;
		this.painter = painter;
		this.mode = mode;
		this.keys = keys;
		this.isHidden = isHidden;
	}

	private shouldAnimate(): boolean {
		if (this.mode.loading) return true;
		if (this.mode.session.active()) return true;
		const [root] = this.mode.pipelineSpans(Date.now());
		return root?.status === "running";
	}

	private syncTimer(): void {
		const wanted = this.shouldAnimate();
		if (wanted && !this.stopTick) {
			const handle = setInterval(() => {
				try {
					this.frame += 1;
					this.tui.requestRender();
				} catch {
					// A repaint failure must never escape into an uncaught exception:
					// extensions share the session process, and a throw here kills it.
				}
			}, SPINNER_TICK_MS);
			(handle as { unref?(): void }).unref?.();
			this.stopTick = () => clearInterval(handle);
		} else if (!wanted && this.stopTick) {
			this.stopTick();
			this.stopTick = undefined;
		}
	}

	render(width: number): string[] {
		if (this.isHidden?.()) return [];
		this.syncTimer();
		const now = Date.now();
		const rows = renderRail(this.mode, this.painter, width, now, this.frame, this.keys);
		// pi-tui skips work when a component returns the same array reference.
		const key = `${width}:${rows.join("\u0000")}`;
		if (key === this.cacheKey) return this.cache;
		this.cacheKey = key;
		this.cache = rows;
		return rows;
	}

	invalidate(): void {
		this.cacheKey = "";
	}

	dispose(): void {
		this.stopTick?.();
		this.stopTick = undefined;
	}
}

/** Compact segment for omp's own status bar, next to model and token counts. */
export function statusSegment(mode: ReviewMode, painter: Painter, now: number): string {
	const item = mode.selected();
	const selCount = mode.selectedKeys.size;
	const modeLabel = `${mode.queueMode === "prs" ? "PR" : "ISS"}${selCount > 0 ? ` [${selCount} sel]` : ""}`;
	const label = painter.fg("accent", `${GLYPH.hex} ${modeLabel}`);
	const position = painter.fg("dim", mode.position());
	if (!item) {
		const state = mode.queueError ? painter.fg("error", "auth") : painter.fg("dim", mode.loading ? "…" : "empty");
		return `${label} ${position} ${state}`;
	}
	const ci = ciGlyph(item.ciStatus);
	const [root] = mode.pipelineSpans(now);
	const stage = root && root.children?.length ? painter.fg(statusRole(root.status), statusIcon(root.status)) : "";
	const title = item.title.length > 28 ? `${item.title.slice(0, 27)}…` : item.title;
	return [label, position, painter.fg("text", `#${item.id}`), painter.fg(statusRole(ci.status), ci.glyph), stage, painter.fg("dim", title)]
		.filter((part) => visibleWidth(part) > 0)
		.join(" ");
}
