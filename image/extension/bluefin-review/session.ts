/**
 * The live omp turn, as a Dagger trace.
 *
 * omp already emits turn and tool-execution events; this projects them into the
 * same span shape the durable pipeline uses, so the dashboard shows what the agent
 * is doing right now above what the appliance recorded earlier — one visual
 * language for both.
 *
 * Bounded by construction: only the last `MAX_TURNS` turns are kept, each turn
 * keeps its tool spans, and each tool span keeps a short log tail.
 */

import type { Span, TraceClass } from "./trace.ts";

const MAX_TURNS = 6;
const MAX_LOG_LINES = 12;

/**
 * A tool call that was cancelled rather than failed. The queue moved on or the
 * maintainer interrupted, so this is normal flow — rendered as `skipped`, not a
 * red `✘`. Detected from the result text: omp surfaces cancellation as a
 * message, and matching a few known words is the cheapest reliable signal.
 */
function isCancellation(result: unknown): boolean {
	const text = typeof result === "string" ? result : JSON.stringify(result ?? "");
	return /cancel|abort|interrupt|no longer needed|superseded/i.test(text);
}

interface ToolArgs {
	command?: unknown;
	path?: unknown;
	pattern?: unknown;
	pull_request?: unknown;
}

/** One-line summary of a tool call, in the style of Dagger's call titles. */
export function describeToolCall(toolName: string, args: unknown): string {
	const record = (args ?? {}) as ToolArgs;
	const first =
		typeof record.command === "string"
			? record.command
			: typeof record.path === "string"
				? record.path
				: typeof record.pattern === "string"
					? record.pattern
					: typeof record.pull_request === "number"
						? `#${record.pull_request}`
						: undefined;
	return first ? `${toolName}(${first.split("\n")[0]})` : `${toolName}()`;
}

function textOf(value: unknown): string[] {
	if (typeof value === "string") return value.split("\n");
	if (Array.isArray(value)) {
		return value.flatMap((entry) => {
			if (entry && typeof entry === "object" && "text" in entry) return textOf((entry as { text?: unknown }).text);
			return [];
		});
	}
	if (value && typeof value === "object") {
		const record = value as { content?: unknown; output?: unknown };
		if (record.content !== undefined) return textOf(record.content);
		if (record.output !== undefined) return textOf(record.output);
	}
	return [];
}

/**
 * Accumulates turn and tool spans for the current session.
 *
 * Every mutator returns void and mutates in place: the dashboard repaints from a
 * timer, so allocating a new tree per streamed token would be pure waste.
 */
export class SessionTrace {
	private turns: Span[] = [];
	private toolsByCallId = new Map<string, Span>();
	private turnCounter = 0;

	/** Roots for the trace pane; newest turn last, matching a log's reading order. */
	roots(): Span[] {
		return this.turns;
	}

	current(): Span | undefined {
		return this.turns[this.turns.length - 1];
	}

	startTurn(now: number): void {
		this.turnCounter += 1;
		const turn: Span = {
			id: `turn/${this.turnCounter}`,
			label: `turn ${this.turnCounter}`,
			status: "running",
			startedAt: now,
			children: [],
		};
		this.turns.push(turn);
		if (this.turns.length > MAX_TURNS) {
			const dropped = this.turns.shift();
			for (const child of dropped?.children ?? []) {
				for (const [id, span] of this.toolsByCallId) if (span === child) this.toolsByCallId.delete(id);
			}
		}
	}

	endTurn(now: number): void {
		const turn = this.current();
		if (!turn) return;
		turn.endedAt = now;
		const failed = (turn.children ?? []).some((child) => child.status === "failure");
		const cancelled = (turn.children ?? []).some((child) => child.status === "skipped");
		turn.status = failed ? "failure" : cancelled ? "skipped" : "success";
		turn.cls = failed ? undefined : cancelled ? ("cancelled" as TraceClass) : undefined;
		for (const child of turn.children ?? []) {
			if (child.status === "running") {
				child.status = "skipped";
				child.endedAt = now;
				child.cls = "cancelled";
			}
		}
	}

	startTool(toolCallId: string, toolName: string, args: unknown, now: number): void {
		let turn = this.current();
		if (!turn || turn.status !== "running") {
			this.startTurn(now);
			turn = this.current();
		}
		if (!turn) return;
		const span: Span = {
			id: `tool/${toolCallId}`,
			label: describeToolCall(toolName, args),
			status: "running",
			startedAt: now,
			logs: [],
		};
		turn.children?.push(span);
		this.toolsByCallId.set(toolCallId, span);
	}

	updateTool(toolCallId: string, partial: unknown): void {
		const span = this.toolsByCallId.get(toolCallId);
		if (!span) return;
		const lines = textOf(partial).filter((line) => line.trim().length > 0);
		if (lines.length === 0) return;
		span.logs = [...(span.logs ?? []), ...lines].slice(-MAX_LOG_LINES);
	}

	endTool(toolCallId: string, result: unknown, isError: boolean, now: number): void {
		const span = this.toolsByCallId.get(toolCallId);
		if (!span) return;
		span.endedAt = now;
		if (isCancellation(result)) {
			// Cancelled is normal, not a failure: skip the span and say why.
			span.status = "skipped";
			span.cls = "cancelled";
		} else {
			span.status = isError ? "failure" : "success";
			span.cls = isError ? ("tool" as TraceClass) : undefined;
		}
		const lines = textOf(result).filter((line) => line.trim().length > 0);
		if (lines.length > 0) span.logs = lines.slice(-MAX_LOG_LINES);
		this.toolsByCallId.delete(toolCallId);
	}

	/** Deepest running span, for the one-line rail under the editor. */
	active(): Span | undefined {
		const turn = this.current();
		if (!turn || turn.status !== "running") return undefined;
		for (let i = (turn.children?.length ?? 0) - 1; i >= 0; i--) {
			const child = turn.children![i]!;
			if (child.status === "running") return child;
		}
		return turn;
	}

	clear(): void {
		this.turns = [];
		this.toolsByCallId.clear();
		this.turnCounter = 0;
	}
}
