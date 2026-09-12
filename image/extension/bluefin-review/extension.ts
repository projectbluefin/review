/**
 * Bluefin review mode for omp: wiring only.
 *
 * Everything here is registration and dispatch — the model lives in `mode.ts`,
 * the pixels in `rail.ts` and `dashboard.ts`. Kept free of `@earendil-works/pi-tui`
 * imports so the whole mode can be driven headlessly by the contract test; the
 * `index.ts` adapter injects the real key matcher.
 *
 * Keyboard only. No slash commands.
 */

import { type DashboardAction, ReviewDashboard } from "./dashboard.ts";
import type { QueueItem } from "./github.ts";
import { DEFAULT_ORG, parseScope, resolveToken } from "./github.ts";
import type { Priority } from "./priority.ts";
import { BATCH_LIMIT, ReviewMode, type PersistedSelection } from "./mode.ts";
import { themePainter } from "./paint.ts";
import { type RailKey, ReviewHitlist, ReviewRail, statusSegment, tmuxReviewStatusBar } from "./rail.ts";
import type { KeyMatcher } from "./keys.ts";
import { type ToolHost, registerTools } from "./tools.ts";
import { BluefinAnsiSplash } from "./splash.ts";
import { HiveLeaderboardComponent } from "./leaderboard.ts";
export const STATE_ENTRY = "com.projectbluefin.review.selection";

/** Queue refetch cadence. GitHub search is rate limited; the state poll is local. */
const QUEUE_POLL_MS = 60_000;
const STATE_POLL_MS = 2_000;
// Hive's queue moves with the project, not with the terminal. Polling it on the
// queue's cadence keeps one hub request per refresh instead of one per repaint.
const HIVE_POLL_MS = 120_000;

export const RAIL_KEYS: readonly RailKey[] = [
	{ chord: "alt+s", label: "autoslay" },
	{ chord: "alt+b", label: "dashboard" },
	{ chord: "alt+j/k", label: "next/prev" },
	{ chord: "alt+x", label: "select" },
	{ chord: "alt+i", label: "prs/issues" },
	{ chord: "alt+o", label: "repo" },
	{ chord: "alt+u", label: "refresh" },
	{ chord: "alt+y", label: "cite" },
];

export interface ExtensionOptions {
	/** Injected by `index.ts` so the overlay understands kitty-protocol chords. */
	matchKey?: KeyMatcher;
	org?: string;
	fetchImpl?: typeof fetch;
	env?: NodeJS.ProcessEnv;
}

/** Loose structural types: the extension must build without omp's declarations. */
interface UiLike {
	notify(message: string, level?: "info" | "warning" | "error"): void;
	input(title: string, placeholder?: string): Promise<string | undefined>;
	setStatus(key: string, value: string | undefined): void;
	setWidget(key: string, content: unknown, options?: { placement?: string }): void;
	setFooter?(factory: ((tui: unknown, theme: unknown, footerData: unknown) => { render(width: number): string[]; invalidate?(): void; dispose?(): void }) | undefined): void;
	setTitle(title: string): void;
	pasteToEditor(text: string): void;
	custom<T>(factory: (tui: unknown, theme: unknown, keybindings: unknown, done: (result: T) => void) => unknown, options?: unknown): Promise<T>;
	readonly theme: { fg(color: string, text: string): string; bold(text: string): string; inverse(text: string): string };
}

interface CtxLike {
	hasUI: boolean;
	ui: UiLike;
	sessionManager?: { getBranch(): Array<{ type?: string; customType?: string; data?: unknown }> };
}

/** The slice of omp's `ExtensionAPI` this mode uses. */
export interface ReviewExtensionHost {
	zod: unknown;
	setLabel(label: string): void;
	on(event: string, handler: (event: unknown, ctx: CtxLike) => unknown): void;
	registerShortcut(chord: string, options: { description?: string; handler: (ctx: CtxLike) => void }): void;
	registerFlag(name: string, options: { description?: string; type: "string" | "boolean"; default?: string | boolean }): void;
	getFlag(name: string): string | boolean | undefined;
	registerTool(definition: unknown): void;
	sendUserMessage(content: string, options?: { deliverAs?: string }): void;
	appendEntry(customType: string, data?: unknown): void;
}

function readPersisted(ctx: CtxLike): PersistedSelection | undefined {
	let latest: PersistedSelection | undefined;
	for (const entry of ctx.sessionManager?.getBranch() ?? []) {
		if (entry.type === "custom" && entry.customType === STATE_ENTRY && entry.data) {
			latest = entry.data as PersistedSelection;
		}
	}
	return latest;
}

/**
 * Prompts the action keys send. Each one names the evidence the agent must use.
 *
 * When Hive ranked the item, the prompt says so and names the queued work it
 * serves: the point of running this tool against an orchestrated project is that
 * the review contributes to what the project decided matters, and an agent that
 * is not told the link cannot honor it.
 */
export function actionPrompt(action: DashboardAction, priority?: Priority): string | undefined {
	const autonomousRule = "This is an automated review and landing tool: execute all actions end-to-end autonomously in a continuous loop. Never ask the user for confirmation, permission, or interactive prompts to proceed. Once a batch or queue item is complete, immediately request the next assignment from the queue or advance to the next item so the loop runs continuously without stopping.";
	const hive = priority?.hiveRank === undefined ? ` ${autonomousRule}` : ` This is Hive-prioritized work (${priority.reason}); keep the linked issue's intent in view and reference it in what you report. ${autonomousRule}`;
	const cite = (item: QueueItem) => `${item.repo}#${item.id} (${item.title})`;
	const batch = "items" in action && action.items && action.items.length > 1 ? action.items : undefined;
	if (batch) {
		// Group items by repository to minimize context-switching and cross-repo tool churn
		const repoGroups = new Map<string, QueueItem[]>();
		for (const it of batch) {
			const list = repoGroups.get(it.repo) ?? [];
			list.push(it);
			repoGroups.set(it.repo, list);
		}
		const isCrossRepo = repoGroups.size > 1;

		let list: string;
		if (isCrossRepo) {
			const sections: string[] = [];
			for (const [repo, items] of repoGroups.entries()) {
				const lines = items.map((it) => `  - #${it.id} (${it.title}): ${it.url}`).join("\n");
				sections.push(`Repository \`${repo}\` (${items.length} item${items.length > 1 ? "s" : ""}):\n${lines}`);
			}
			list = sections.join("\n\n");
		} else {
			list = batch.map((it) => `- ${cite(it)}: ${it.url}`).join("\n");
		}

		const crossRepoHeader = isCrossRepo
			? `These ${batch.length} items span ${repoGroups.size} repositories (${[...repoGroups.keys()].join(", ")}).`
			: "";

		// The point of selecting a slice is to spend one wall clock on all of it.
		// A batch worked top to bottom is a list, not a batch, and a backlog that
		// only moves at one item per turn never comes down.
		// Dispatch one subagent per individual issue or PR, capped at a maximum of 7 concurrent
		// subagents at any time (queue remaining items and dispatch as running slots free up).
		// Review agents do not count toward this cap so they can take their time.
		// When subagents complete, the review agent (k3-final-review) clumps by repository:
		// for each repository cohort (e.g. 10 issues in bluefin), it waits for that repo's queue
		// to finish, then audits, consolidates all changes, and lands them together into ONE PR per repo.
		const fanOut = `Work all ${batch.length} items with ONE subagent per issue/PR, capped at a maximum of 7 concurrent subagents at any time (queue remaining items and dispatch as running subagents complete; review/landing agents do not count against the 7 cap). Each subagent owns exactly its assigned item. Tell every subagent to skip formatters, linters, and project-wide suites and run only the smallest existing test covering what changed. Report per item — what you did, the evidence, and the outcome.`;

		const auditInstruction = `When issues/PRs are worked, repository clumping happens at the review agent level: for each repository (e.g. all items in \`${[...repoGroups.keys()].join("`, `")}\`), dispatch one \`k3-final-review\` subagent (Kimi K3 at max effort; review agents do not consume the 7 cap). If the review agent has to wait for that repository's queue to finish through the 7-subagent cap, it waits. Once that repository cohort finishes, the review agent audits, consolidates all changes, and lands them all in one PR per repository, verifying cross-repository contract compatibility, shared schema and dependency alignment, doctrine invariants, and simplicity.`;

		const autonomousRule = "This is an automated review and landing tool: execute all actions end-to-end autonomously in a continuous loop. Never ask the user for confirmation, permission, or interactive prompts to proceed. Once a batch or queue item is complete, immediately request the next assignment from the queue or advance to the next item so the loop runs continuously without stopping.";
		const protocol = `${fanOut}\n\n${auditInstruction}\n\n${autonomousRule}`;
		switch (action.kind) {
			case "review":
				return `Review the following ${batch.length} selected items grouped by repository for efficiency:\n\n${list}\n\n${crossRepoHeader ? `${crossRepoHeader}\n\n` : ""}For each repository group: read bounded diffs and recorded pipelines before judging. Report findings by severity with file:line evidence covering doctrine, correctness, security, tests, and simplicity. State explicitly what you verified and what you could not.\n\n${protocol}`;
			case "diff":
				return `Inspect and summarize the diffs for the following ${batch.length} selected items grouped by repository:\n\n${list}\n\n${crossRepoHeader ? `${crossRepoHeader}\n\n` : ""}For each repository group, call bluefin_review_diff and summarize what changed file by file, with the cross-repo risk each change carries.\n\n${autonomousRule}`;
			case "docs":
				return `Update and align documentation for the following ${batch.length} selected items grouped by repository:\n\n${list}\n\n${crossRepoHeader ? `${crossRepoHeader}\n\n` : ""}Enforce the projectbluefin/common agentic documentation system with brutal alignment: ensure AGENTS.md, docs/factory/agentic-model.md, docs/SKILL.md, and docs/skills/*.md are strictly source-backed, concise (<200 lines soft max, <256 char descriptions), zero-filler, with no grandfathering or speculative noise. Run \`bash scripts/check-skill-frontmatter.sh --write\` and ensure \`docs/skills/index.json\` is regenerated cleanly.\n\n${protocol}`;
			case "approve":
				return `For the following ${batch.length} selected items grouped by repository:\n\n${list}\n\n${crossRepoHeader ? `${crossRepoHeader}\n\n` : ""}Confirm every required check is green per repository, restate the merge risk and cross-repo dependencies, then approve and squash merge in dependency order. Stop and report if any check is failing or pending.\n\n${protocol}`;
			case "fix":
				return `Fix the findings recorded for the following ${batch.length} selected items grouped by repository:\n\n${list}\n\n${crossRepoHeader ? `${crossRepoHeader}\n\n` : ""}For each repository, read them with bluefin_review_trace, address each at its source, run the smallest contract test covering the changed surface, and prepare clean commits.\n\n${protocol}`;
			case "slay":
				// Issues have no diff to land. Slaying one means producing the change
				// it asked for and handing it to a human as a pull request.
				return batch.every((entry) => entry.type === "issue")
					? `Close out the following ${batch.length} queued issues by shipping the work, one pull request per issue:\n\n${list}\n\n${crossRepoHeader ? `${crossRepoHeader}\n\n` : ""}For each issue: do not dismiss or conclude no_work_needed if there is an actionable bug, missing test, broken script, or underlying root cause to address. Diagnose the root cause, implement the fix, run the smallest existing test covering the changed surface, and open a pull request that closes it with \`Closes <owner/repo>#<number>\` in the body. Someone else reviews and merges: never merge your own, never approve. Only if an issue has genuinely already been merged by an earlier PR on the default branch: confirm that commit and close the issue directly with \`gh issue close <number> --repo <owner/repo> --reason completed --comment "<evidence of prior merged PR>"\`. Where an issue cannot be finished as asked, open no pull request for it and report an evidenced finding instead, naming what blocked you.\n\n${protocol}`
					: `Execute the full fix-and-merge landing pass on the following ${batch.length} selected items:\n\n${list}\n\n${crossRepoHeader ? `${crossRepoHeader}\n\n` : ""}For each PR: review the diff, patch defects directly at source, fix failing tests, verify with focused contract tests, re-kick flaky CI checks (\`gh run rerun <run-id> --failed\`), and once checks are green, approve and squash-merge the pull request with \`gh pr review <id> --repo <repo> --approve\` and \`gh pr merge <id> --repo <repo> --squash\` (or \`--auto --squash\` plus \`lgtm\` label if governed by a merge queue ruleset). Do not leave actionable PRs unmerged once green. Once landed or blocked, immediately proceed to the next assignment.\n\n${protocol}`;
			default:
				break;
		}
	}
	switch (action.kind) {
		case "review":
			return `Review ${cite(action.item)}. Read the bounded diff with bluefin_review_diff and the recorded pipeline with bluefin_review_trace before judging. Report findings by severity with file:line evidence, covering doctrine, correctness, security, tests, and simplicity. State explicitly what you verified and what you could not.${hive}`;
		case "diff":
			return `Call bluefin_review_diff for pull request ${action.item.id} in ${action.item.repo} and summarise what actually changed, file by file, with the risk each change carries. ${autonomousRule}`;
		case "docs":
			return `Update and align documentation for ${cite(action.item)}. Enforce the projectbluefin/common agentic documentation system with brutal alignment: inspect the actual diff and changed surface, update the closest matching docs/skills/*.md file or core contract (AGENTS.md, docs/factory/agentic-model.md, docs/SKILL.md), eliminate any grandfathering/speculative filler, enforce token efficiency (descriptions <= 256 chars, skill documents <= 200 lines soft max), and run \`bash scripts/check-skill-frontmatter.sh --write\` to ensure docs/skills/index.json is synchronized perfectly for token-efficient agent ingestion. ${autonomousRule}`;
		case "approve":
			return `For ${cite(action.item)}: confirm every required check is green with \`gh pr checks ${action.item.id} --repo ${action.item.repo}\`, restate the merge risk in one line, then approve with \`gh pr review ${action.item.id} --repo ${action.item.repo} --approve\`. Attempt squash merge with \`gh pr merge ${action.item.id} --repo ${action.item.repo} --squash\`; if the repository uses a merge queue or ruleset, enable auto-merge (\`gh pr merge ${action.item.id} --repo ${action.item.repo} --auto --squash\`) and ensure the \`lgtm\` label is present (\`gh pr edit ${action.item.id} --repo ${action.item.repo} --add-label lgtm\`). Stop and report instead of merging if any check is failing or pending. ${autonomousRule}`;
		case "fix":
			return `Fix the findings recorded for ${cite(action.item)}. Read them with bluefin_review_trace, address each one at its source, run the smallest contract test that covers the changed surface, and prepare one clean commit. Do not suppress a finding you cannot fix — report it.${hive}`;
		case "slay":
			// Issues have no diff to land. Slaying one means producing the change it
			// asked for and handing it to a human as a pull request.
			return action.item.type === "issue"
				? `Close out ${cite(action.item)} by implementing and shipping the solution. Do not dismiss or conclude with no_work_needed if there is any actionable bug, test failure, code change, documentation fix, or underlying root cause to address. Inspect the code, diagnose the problem, implement the fix, run the smallest existing test that covers the changed surface, then open a pull request against the default branch whose body contains \`Closes ${action.item.repo}#${action.item.id}\`. Someone else reviews and merges it: never merge your own, never approve it. Only if the issue has already been resolved or closed by an existing merged PR or commit on the default branch: confirm the evidence and close the issue directly with \`gh issue close ${action.item.id} --repo ${action.item.repo} --reason completed --comment "<evidence of live resolution or commit>"\`. Otherwise implement what it asks for and open the PR.${hive}`
				: `Execute the full fix-and-merge landing pass on ${cite(action.item)}: review the diff, patch what is broken, fix and commit any failing tests or defects, ensure contract tests pass, re-kick transient CI failures (\`gh run rerun <run-id> --failed\`), and as soon as checks are green, approve and land the pull request: approve with \`gh pr review ${action.item.id} --repo ${action.item.repo} --approve\`, squash-merge with \`gh pr merge ${action.item.id} --repo ${action.item.repo} --squash\` (or enable auto-merge \`gh pr merge ${action.item.id} --repo ${action.item.repo} --auto --squash\` if using a merge queue), and apply \`lgtm\` label if required by branch protection/rulesets (\`gh pr edit ${action.item.id} --repo ${action.item.repo} --add-label lgtm\`). Once merged or if blocked by policy, advance immediately to the next queue assignment.${hive}`;
		case "snapshot":
			return `Submit the Argo workflow in deploy/argo-review-fsdk-build.yaml to build and push a container snapshot of the current tree, then report the workflow name and how to watch it.`;
		default:
			return undefined;
	}
}

/**
 * What the caller keeps after wiring the mode into a host.
 *
 * `session_start` returns before its own work is finished, so "the session has
 * started" and "the queue is on screen" are two different moments. Anything that
 * needs the second one — a test, a headless caller — awaits this.
 */
export interface ReviewExtension {
	whenStarted(): Promise<void>;
}

export function createReviewExtension(pi: ReviewExtensionHost, options: ExtensionOptions = {}): ReviewExtension {
	const env = options.env ?? process.env;
	const matchKey = options.matchKey;
	const mode = new ReviewMode({
		org: options.org ?? env.BLUEFIN_REVIEW_ORG ?? DEFAULT_ORG,
		fetchImpl: options.fetchImpl,
		// The mode resolves the hub from this environment too; leaving it to
		// process.env is how a test reads the developer's own registration.
		env,
	});

	let tui: { requestRender(): void } | undefined;
	const timers: Array<() => void> = [];
	let dashboardOpen = false;
	let autoReopenDashboard = false;
	let autoslayActive = false;
	let activeDashboardDone: ((action: DashboardAction) => void) | undefined;
	let activeCtx: CtxLike | undefined;
	let started: Promise<void> = Promise.resolve();
	pi.setLabel("Bluefin Review");
	pi.registerFlag("pr", { description: "Preselect a pull request or issue number", type: "string" });
	pi.registerFlag("issues", { description: "Start in issues mode instead of pull requests", type: "boolean", default: false });
	pi.registerFlag("all", { description: "Show all queue items instead of defaulting to Hive-only", type: "boolean", default: false });
	pi.registerFlag("splash", { description: "Show 1990s demoscene Razor 1911 ANSI splash screen", type: "boolean", default: true });
	pi.registerFlag("repo", { description: "Review one repository: owner/repo, or org:name for a whole organization", type: "string" });
	pi.registerFlag("skip-repo", { description: "Comma-separated repositories to skip (e.g. lab, projectbluefin/lab)", type: "string" });
	pi.registerFlag("autoslay", { description: "Autoslay queue continuously in Hive priority order on startup", type: "boolean", default: false });
	registerTools(pi as unknown as ToolHost, mode, () => started);

	const repaint = () => tui?.requestRender();

	const syncStatus = (ctx: CtxLike) => {
		if (!ctx.hasUI) return;
		if (dashboardOpen) {
			ctx.ui.setStatus("bluefin_queue", undefined);
		} else {
			ctx.ui.setStatus("bluefin_queue", statusSegment(mode, themePainter(ctx.ui.theme), Date.now()));
		}
		const activeItem = mode.selected();
		if (activeItem) {
			const kind = activeItem.type === "pr" ? "PR" : "ISSUE";
			const repo = activeItem.repo.includes("/") ? activeItem.repo.split("/")[1] : activeItem.repo;
			ctx.ui.setTitle(`bluefin review · ${kind} #${activeItem.id} (${repo}) ${activeItem.title}`);
		} else {
			ctx.ui.setTitle(`bluefin review · ${mode.queueMode} (${mode.position()})`);
		}
		repaint();
	};

	const every = (intervalMs: number, work: () => void) => {
		const handle = setInterval(() => {
			try {
				work();
			} catch {
				// Extensions share the session process; a throw from a timer is fatal.
			}
		}, intervalMs);
		(handle as { unref?(): void }).unref?.();
		timers.push(() => clearInterval(handle));
	};

	const refreshQueue = async (ctx: CtxLike) => {
		syncStatus(ctx);
		const result = await mode.refreshQueue();
		if (result.error && !result.cancelled && result.items.length === 0 && ctx.hasUI) {
			ctx.ui.notify(`Bluefin queue: ${result.error}`, "error");
		}
		syncStatus(ctx);
	};

	const persist = () => pi.appendEntry(STATE_ENTRY, mode.toPersisted());

	/**
	 * Point the queue at another repository.
	 *
	 * Accepts `owner/repo`, a bare repository name in the configured
	 * organization, a GitHub URL, or `org:<name>` to go back to a whole
	 * organization. Anything else is rejected rather than silently searched for.
	 */
	const promptForScope = async (ctx: CtxLike): Promise<boolean> => {
		if (!ctx.hasUI) return false;
		const answer = await ctx.ui.input("Review which repository?", "owner/repo, or org:name");
		if (answer === undefined || !answer.trim()) return false;
		const scope = parseScope(answer, mode.org);
		if (!scope) {
			ctx.ui.notify(`Not a repository: ${answer.trim()}`, "error");
			return false;
		}
		mode.setScope(scope);
		ctx.ui.notify(`Queue scoped to ${mode.scopeLabel()}`, "info");
		await refreshQueue(ctx);
		persist();
		return true;
	};

	const dispatch = async (ctx: CtxLike, action: DashboardAction, options?: { deliverAs?: "steer" | "followUp" }): Promise<void> => {
		if (action.kind === "close") {
			autoReopenDashboard = false;
			return;
		}
		if (action.kind === "scope") {
			await promptForScope(ctx);
			return;
		}
		if (action.kind === "reference") {
			const items = "items" in action && action.items && action.items.length > 0 ? action.items : [action.item];
			const text = items.map((it) => `${it.repo}#${it.id} — ${it.title}\n${it.url}\n`).join("\n");
			ctx.ui.pasteToEditor(text);
			return;
		}
		const count = "items" in action && action.items && action.items.length > 1 ? action.items.length : 1;
		const priority = action.kind === "snapshot" ? undefined : mode.priorityFor(action.item);
		const prompt = actionPrompt(action, priority);
		if (!prompt) return;
		const label = count > 1 ? `${action.kind}: ${count} items` : `${action.kind}: ${action.item.repo}#${action.item.id}`;
		ctx.ui.notify(action.kind === "snapshot" ? "Queuing snapshot build…" : label, "info");
		activeCtx = ctx;
		autoReopenDashboard = !autoslayActive;
		mode.clearSelected();
		pi.sendUserMessage(prompt, options?.deliverAs ? { deliverAs: options.deliverAs } : undefined);
	};
	const openLeaderboard = async (ctx: CtxLike) => {
		if (!ctx.hasUI) return;
		try {
			await ctx.ui.custom<void>(
				(hostTui, theme, _keybindings, done) => {
					return new HiveLeaderboardComponent(
						hostTui as { requestRender(): void },
						themePainter(theme as UiLike["theme"]),
						done,
					);
				},
				{ overlay: false },
			);
		} catch {
			// Ignore cancellation
		}
	};

	const openDashboard = async (ctx: CtxLike) => {
		if (!ctx.hasUI || dashboardOpen) return;
		dashboardOpen = true;
		mode.refreshState();
		try {
			const action = await ctx.ui.custom<DashboardAction>(
				(hostTui, theme, _keybindings, done) => {
					tui = hostTui as { requestRender(): void };
					activeDashboardDone = done;
					return new ReviewDashboard(
						tui,
						themePainter(theme as UiLike["theme"]),
						mode,
						done,
						() => void refreshQueue(ctx),
						Math.max(14, Math.min(30, (process.stdout.rows ?? 30) - 8)),
						matchKey,
					);
				},
				{ overlay: false },
			);
			if (action.kind === "slay") {
				autoslayActive = true;
			}
			await dispatch(ctx, action);
			// looking at the queue you just asked for.
			if (action.kind === "scope") {
				dashboardOpen = false;
				await openDashboard(ctx);
				return;
			}
			if (action.kind === "leaderboard") {
				dashboardOpen = false;
				await openLeaderboard(ctx);
				await openDashboard(ctx);
				return;
			}
		} catch {
			// The overlay was cancelled. Nothing awaits this call, so a rejection
			// here would surface as an unhandled rejection, not a closed dashboard.
		} finally {
			dashboardOpen = false;
			activeDashboardDone = undefined;
			persist();
			syncStatus(ctx);
		}
	};

	/**
	 * Everything startup does that is not instantaneous.
	 *
	 * omp kills an extension handler that has not returned inside its budget, and
	 * this is two network round trips plus an animated intro. Run inside
	 * `session_start` it timed out every session: the poll timers below it never
	 * started, so the queue was fetched once, at most, and never refreshed.
	 */
	const startSession = async (ctx: CtxLike, persisted: PersistedSelection | undefined) => {
		// Started, not awaited: the intro plays over the fetch instead of after it.
		const splash =
			pi.getFlag("splash") === false
				? undefined
				: ctx.ui.custom<void>(
						(hostTui, _theme, _keybindings, done) => {
							return new BluefinAnsiSplash(hostTui as { requestRender(): void }, done);
						},
						{ overlay: false },
					);

		// Ask the hub before the queue: an item that arrives already ranked is
		// never shown in the wrong order, not even for one frame.
		const hive = await mode.refreshHive();
		if (hive.configured && hive.error) {
			ctx.ui.notify(`Hive unreachable, ordering locally: ${hive.error}`, "warning");
		}
		await refreshQueue(ctx);
		mode.restore(persisted);

		const preselect = pi.getFlag("pr");
		if (typeof preselect === "string" && preselect.trim()) {
			const number = Number.parseInt(preselect.trim().replace(/^#/, ""), 10);
			if (Number.isInteger(number) && !mode.selectById(undefined, number)) {
				ctx.ui.notify(`#${number} is not in the open ${mode.queueMode} queue`, "warning");
			}
		}
		syncStatus(ctx);

		await splash;
		const flagAutoslay = pi.getFlag("autoslay");
		if (flagAutoslay === true) {
			autoslayActive = true;
			const slayable = mode.slayableItems();
			const items = (slayable.length > 0 ? slayable.slice(0, 7) : [mode.selected()].filter(Boolean)) as QueueItem[];
			if (items.length > 0) {
				const action: DashboardAction = { kind: "slay", item: items[0]!, items: items.length > 1 ? items : undefined };
				void dispatch(ctx, action);
				return;
			}
			autoslayActive = false;
		}
		// Opened, not awaited: `ctx.ui.custom` resolves when the maintainer closes
		// the dashboard, and startup is over long before that.
		void openDashboard(ctx);
	};

	pi.on("session_start", async (_event, ctx) => {
		mode.setToken(resolveToken(env));
		// Mode, scope and filter apply immediately; the remembered item can only be
		// found once the queue has actually been fetched, so restore runs twice.
		const persisted = readPersisted(ctx);
		mode.restore(persisted);

		const flagIssues = pi.getFlag("issues");
		if (flagIssues === true) mode.queueMode = "issues";

		const flagAll = pi.getFlag("all");
		if (flagAll === true) mode.hiveOnly = false;
		// An explicit scope beats a remembered one: you asked for it on the
		// command line, this run.
		const flagRepo = pi.getFlag("repo");
		if (typeof flagRepo === "string" && flagRepo.trim()) {
			const scope = parseScope(flagRepo, mode.org);
			if (scope) mode.setScope(scope);
			else if (ctx.hasUI) ctx.ui.notify(`--repo is not a repository: ${flagRepo}`, "error");
		}
		const flagSkipRepo = pi.getFlag("skip-repo");
		if (typeof flagSkipRepo === "string" && flagSkipRepo.trim()) {
			for (const r of flagSkipRepo.split(",")) {
				const trimmed = r.trim().toLowerCase();
				if (trimmed) mode.skipRepos.add(trimmed);
			}
		}
		if (!ctx.hasUI) {
			// No UI, so no frame can show an unranked queue: the two reads race
			// safely, and both reprioritize on arrival. Nothing is awaited here
			// either — the queue tools await `started` themselves, which is what a
			// headless caller actually needs and what the handler budget allows.
			started = Promise.all([mode.refreshHive(), refreshQueue(ctx)])
				.then(() => {
					mode.restore(persisted);
				})
				.catch(() => {
					// fetchHive and fetchQueue report failure in their results; a
					// throw here must still leave `started` resolvable for the tools.
				});
			return;
		}

		ctx.ui.setTitle("bluefin review");
		ctx.ui.setWidget(
			"bluefin-rail",
			(hostTui: unknown, theme: unknown) => {
				tui = hostTui as { requestRender(): void };
				return new ReviewRail(tui, themePainter(theme as UiLike["theme"]), mode, RAIL_KEYS, () => dashboardOpen);
			},
			{ placement: "belowEditor" },
		);

		if (typeof ctx.ui.setFooter === "function") {
			ctx.ui.setFooter((_hostTui: unknown, theme: unknown) => {
				const painter = themePainter(theme as UiLike["theme"]);
				return {
					render(width: number): string[] {
						return [tmuxReviewStatusBar(mode, painter, width, Date.now())];
					},
				};
			});
		}

		mode.refreshState();
		syncStatus(ctx);

		// Before the first await: a startup that fails or drags must still leave a
		// session that refreshes itself.
		every(STATE_POLL_MS, () => {
			if (mode.refreshState()) repaint();
		});
		every(QUEUE_POLL_MS, () => {
			void refreshQueue(ctx);
		});
		every(HIVE_POLL_MS, () => {
			void mode.refreshHive().then(() => {
				syncStatus(ctx);
			});
		});

		// Detached: nothing awaits this, so an escaping rejection would take the
		// whole session process down with it.
		started = startSession(ctx, persisted).catch((error: unknown) => {
			ctx.ui.notify(`Bluefin review startup: ${error instanceof Error ? error.message : String(error)}`, "error");
		});
	});

	pi.on("session_shutdown", () => {
		for (const stop of timers.splice(0)) stop();
	});

	// ---- live turn trace -----------------------------------------------------

	pi.on("turn_start", () => {
		mode.session.startTurn(Date.now());
		repaint();
	});
	pi.on("turn_end", async (_event, eventCtx) => {
		mode.session.endTurn(Date.now());
		repaint();
		const ctxToUse = (eventCtx as CtxLike | undefined) ?? activeCtx;
		if (autoslayActive && ctxToUse) {
			await refreshQueue(ctxToUse);
			const nextBatch = mode.slayableItems();
			if (nextBatch.length > 0) {
				const items = nextBatch.slice(0, 7);
				const action: DashboardAction = { kind: "slay", item: items[0]!, items: items.length > 1 ? items : undefined };
				void dispatch(ctxToUse, action, { deliverAs: "followUp" });
				return;
			}
			autoslayActive = false;
			if (ctxToUse.hasUI) ctxToUse.ui.notify("Autoslay completed: queue fully drained", "info");
		}
		if (autoReopenDashboard && ctxToUse && ctxToUse.hasUI && !dashboardOpen) {
			autoReopenDashboard = false;
			await refreshQueue(ctxToUse);
			if (mode.visibleItems().length > 0) {
				void openDashboard(ctxToUse);
			}
		}
	});
	pi.on("agent_settled", async (_event, eventCtx) => {
		const ctxToUse = (eventCtx as CtxLike | undefined) ?? activeCtx;
		if (autoslayActive && ctxToUse) {
			await refreshQueue(ctxToUse);
			const nextBatch = mode.slayableItems();
			if (nextBatch.length > 0) {
				const items = nextBatch.slice(0, 7);
				const action: DashboardAction = { kind: "slay", item: items[0]!, items: items.length > 1 ? items : undefined };
				void dispatch(ctxToUse, action);
				return;
			}
			autoslayActive = false;
			if (ctxToUse.hasUI) ctxToUse.ui.notify("Autoslay completed: queue fully drained", "info");
		}
	});
	pi.on("tool_execution_start", (event) => {
		const { toolCallId, toolName, args } = event as { toolCallId: string; toolName: string; args: unknown };
		mode.session.startTool(toolCallId, toolName, args, Date.now());
		repaint();
	});
	pi.on("tool_execution_update", (event) => {
		const { toolCallId, partialResult } = event as { toolCallId: string; partialResult: unknown };
		mode.session.updateTool(toolCallId, partialResult);
		repaint();
	});
	pi.on("tool_execution_end", (event) => {
		const { toolCallId, result, isError } = event as { toolCallId: string; result: unknown; isError: boolean };
		mode.session.endTool(toolCallId, result, isError === true, Date.now());
		repaint();
	});

	// ---- keyboard ------------------------------------------------------------

	pi.registerShortcut("alt+b", {
		description: "Open the Bluefin review dashboard",
		handler: (ctx) => void openDashboard(ctx),
	});
	pi.registerShortcut("alt+j", {
		description: "Select the next queue item",
		handler: (ctx) => {
			mode.move(1);
			syncStatus(ctx);
		},
	});
	pi.registerShortcut("alt+k", {
		description: "Select the previous queue item",
		handler: (ctx) => {
			mode.move(-1);
			syncStatus(ctx);
		},
	});
	pi.registerShortcut("alt+x", {
		description: "Toggle selection on current queue item",
		handler: (ctx) => {
			const item = mode.selected();
			if (!item) return;
			const nowSelected = mode.toggleSelected();
			if (ctx.hasUI) {
				const state = nowSelected ? "selected" : "deselected";
				const count = mode.selectedKeys.size;
				ctx.ui.notify(`${state} #${item.id} (${count} selected)`, "info");
			}
			syncStatus(ctx);
		},
	});
	pi.registerShortcut("alt+i", {
		description: "Toggle pull requests and issues",
		handler: (ctx) => {
			const next = mode.toggleMode();
			if (ctx.hasUI) ctx.ui.notify(`Bluefin queue: ${next === "prs" ? "pull requests" : "issues"}`, "info");
			void refreshQueue(ctx);
			persist();
		},
	});
	pi.registerShortcut("alt+o", {
		description: "Review another repository (owner/repo)",
		handler: (ctx) => void promptForScope(ctx),
	});
	pi.registerShortcut("alt+u", {
		description: "Refetch the Bluefin queue",
		handler: (ctx) => void refreshQueue(ctx),
	});
	pi.registerShortcut("alt+y", {
		description: "Cite the selected queue item in the prompt",
		handler: (ctx) => {
			const chosen = mode.chosenItems();
			const items = chosen.length > 0 ? chosen : [mode.selected()].filter(Boolean) as QueueItem[];
			if (items.length === 0) {
				if (ctx.hasUI) ctx.ui.notify("No queue item selected", "warning");
				return;
			}
			const text = items.map((item) => `${item.repo}#${item.id} — ${item.title}\n${item.url}\n`).join("\n");
			ctx.ui.pasteToEditor(text);
		},
	});
	pi.registerShortcut("alt+s", {
		description: "Autoslay queue in Hive priority order (continuous serial loop)",
		handler: (ctx) => {
			// Autoslay runs directly in strict Hive priority order across the queue,
			// cycling continuously through assignments without requiring manual intervention.
			autoslayActive = true;
			const slayable = mode.slayableItems();
			const items = (slayable.length > 0 ? slayable.slice(0, 7) : [mode.selected()].filter(Boolean)) as QueueItem[];
			if (items.length === 0) {
				autoslayActive = false;
				if (ctx.hasUI) ctx.ui.notify("No queue items available to slay", "warning");
				return;
			}
			const action: DashboardAction = { kind: "slay", item: items[0]!, items: items.length > 1 ? items : undefined };
			if (dashboardOpen && activeDashboardDone) {
				activeDashboardDone(action);
			} else {
				void dispatch(ctx, action);
			}
		},
	});

	return { whenStarted: () => started };
}
