/**
 * LLM-callable tools.
 *
 * Each one returns real data or an explicit failure. A tool that claims to have
 * fetched a diff and returns prose is worse than no tool: the model believes it.
 */

import { diffToText, fetchDiff } from "./github.ts";
import { fetchHiveMe, hiveFailureStatus } from "./hive.ts";
import { fetchHiveLeaderboard, getTierInfo } from "./leaderboard.ts";
import { queueKey } from "./state.ts";
import { traceToText } from "./trace.ts";

interface ToolContent {
	type: "text";
	text: string;
}

interface ToolResult {
	content: ToolContent[];
	details?: unknown;
	isError?: boolean;
}

interface ZodLike {
	object(shape: Record<string, unknown>): unknown;
	string(): { optional(): unknown; describe(text: string): { optional(): unknown } };
	number(): { describe(text: string): { optional(): unknown } };
}

export interface ToolHost {
	zod: ZodLike;
	registerTool(definition: {
		name: string;
		label: string;
		description: string;
		parameters: unknown;
		execute(toolCallId: string, params: Record<string, unknown>): Promise<ToolResult>;
	}): void;
}

function text(value: string): ToolContent[] {
	return [{ type: "text", text: value }];
}

/** Resolve the repository for a tool call: explicit, else the selection, else org default. */
function resolveRepo(mode: ReviewMode, params: Record<string, unknown>): string | undefined {
	const explicit = typeof params.repo === "string" ? params.repo : undefined;
	if (explicit) return explicit.includes("/") ? explicit : `${mode.org}/${explicit}`;
	const number = typeof params.pull_request === "number" ? params.pull_request : undefined;
	const match = number === undefined ? mode.selected() : mode.items.find((item) => item.id === number);
	return match?.repo;
}


/**
 * One sentence naming the authority that produced the order.
 *
 * "Hive ordered this", "Hive had nothing to say about this scope" and "we could
 * not reach Hive" are three different facts, and a model told the wrong one will
 * either ignore a priority that exists or invent one that does not.
 */
function orderLine(mode: ReviewMode): string {
	const hive = mode.hive;
	if (!hive.configured) {
		return "order: local — no hive hub configured; queue classified from live GitHub evidence";
	}
	if (!hive.online) {
		return `order: local — ${hiveFailureStatus(hive.error)}, configured but unreachable`;
	}
	const actionable = hive.actionableItems === undefined ? "" : `, ${hive.actionableItems} actionable overall`;
	const coverage = mode.hiveCoverage();
	if (mode.orderSource() !== "hive") {
		return `order: local — hive is online at ${hive.hub} but has queued nothing in this scope${actionable}`;
	}
	const shortfall =
		coverage.present < coverage.total
			? ` ${coverage.total - coverage.present} of Hive's ${coverage.total} queued items could not be resolved on GitHub and are missing from this queue.`
			: "";
	return `order: hive — ${mode.hiveRankedCount()} of ${mode.items.length} items ranked by ${hive.hub}${actionable}. Hive owns priority; do not reorder or reassign it.${shortfall}`;
}

/**
 * @param whenReady Resolves once startup has fetched the hub and the queue.
 *   `session_start` returns before that, so a tool called early would otherwise
 *   report an empty queue as though the organization had nothing open.
 */
export function registerTools(pi: ToolHost, mode: ReviewMode, whenReady: () => Promise<void>): void {
	const z = pi.zod;

	pi.registerTool({
		name: "bluefin_review_status",
		label: "Review Status",
		description:
			"Current Bluefin review queue: mode, counts, the selected item, and the durable pipeline trace recorded for it (run state, review events, landing events, receipt findings).",
		parameters: z.object({}),
		async execute() {
			await whenReady();
			const now = Date.now();
			const item = mode.selected();
			const tally = mode.ciTally();
			const priority = item ? mode.priorityFor(item) : undefined;
			const hive = mode.hive;
			const lines = [
				`mode=${mode.queueMode} scope=${mode.scopeLabel()} items=${mode.visibleItems().length}/${mode.items.length}`,
				`ci: ${tally.success} passing, ${tally.failure} failing, ${tally.pending} pending, ${tally.unknown} unknown`,
				orderLine(mode),
			];
			if (hive.online && hive.triage.length > 0) {
				lines.push(`triage: ${hive.triage.map((group) => `${group.label} ${group.count}`).join(", ")}`);
			}
			if (mode.queueError) lines.push(`queue error: ${mode.queueError}`);
			if (item) {
				lines.push(
					"",
					`selected ${item.repo}#${item.id} — ${item.title}`,
					`author @${item.author}${item.draft ? " (draft)" : ""} ci=${item.ciStatus ?? "unknown"} merge=${item.mergeState} review=${item.reviewState} labels=${item.labels.join(",") || "none"}`,
					priority ? `priority: ${priority.category} (${priority.reason}, ${priority.source})` : "priority: unranked",
					item.url,
					"",
					traceToText(mode.pipelineSpans(now), now),
				);
			} else {
				lines.push("", "no item selected");
			}

			return {
				content: text(lines.join("\n")),
				details: {
					mode: mode.queueMode,
					org: mode.org,
					scope: mode.scope,
					order_source: mode.orderSource(),
					hive: {
						configured: hive.configured,
						online: hive.online,
						hub: hive.hub || null,
						ranked: mode.hiveRankedCount(),
						queued: mode.hiveCoverage(),
						actionable_items: hive.actionableItems ?? null,
						triage: hive.triage,
						error: hive.error ?? null,
					},
					selected_priority: priority ?? null,
					total_items: mode.items.length,
					visible_items: mode.visibleItems().length,
					queue_error: mode.queueError ?? null,
					ci: tally,
					selected: item ?? null,
					state_root: mode.stateRoot,
				},
			};
		},
	});

	pi.registerTool({
		name: "bluefin_review_queue",
		label: "Review Queue",
		description:
			"List the live Project Bluefin queue of open pull requests or issues, optionally filtered by a substring across title, repository, author, label, and number.",
		parameters: z.object({
			filter: z.string().describe("substring matched against title, repo, author, labels, number").optional(),
			limit: z.number().describe("maximum rows to return (default 30)").optional(),
		}),
		async execute(_id, params) {
			await whenReady();
			const limit = typeof params.limit === "number" ? Math.max(1, Math.min(100, params.limit)) : 30;
			const needle = typeof params.filter === "string" ? params.filter.toLowerCase() : "";
			const rows = mode.visibleItems()
				.filter(
					(item) =>
						!needle ||
						item.title.toLowerCase().includes(needle) ||
						item.repo.toLowerCase().includes(needle) ||
						item.author.toLowerCase().includes(needle) ||
						String(item.id).includes(needle) ||
						item.labels.some((label) => label.toLowerCase().includes(needle)),
				)
				.slice(0, limit);

			if (rows.length === 0) {
				return {
					content: text(mode.queueError ? `queue unavailable: ${mode.queueError}` : "no matching items"),
					details: { items: [], queue_error: mode.queueError ?? null },
					isError: Boolean(mode.queueError),
				};
			}

			const body = rows
				.map((item) => {
					const priority = mode.priorityFor(item);
					const chip = priority ? `[${priority.category}]` : "[unranked]";
					return `${chip} ${item.repo}#${item.id} [ci:${item.ciStatus ?? "unknown"}] ${item.title} (@${item.author})`;
				})
				.join("\n");
			return {
				content: text(body),
				details: {
					items: rows,
					order_source: mode.orderSource(),
					priorities: Object.fromEntries(
						rows.map((item) => [`${item.repo}#${item.id}`, mode.priorityFor(item) ?? null]),
					),
					queue_error: mode.queueError ?? null,
				},
			};
		},
	});

	pi.registerTool({
		name: "bluefin_review_diff",
		label: "Review Diff",
		description:
			"Fetch the bounded diff for a pull request from the GitHub API: every changed file with add/delete counts, and patch text for the first files up to a character budget.",
		parameters: z.object({
			pull_request: z.number().describe("Pull request number to inspect"),
			repo: z.string().describe("owner/repo; defaults to the selected item's repository").optional(),
			max_files: z.number().describe("files whose patch text is included (default 20)").optional(),
		}),
		async execute(_id, params) {
			const pullRequest = typeof params.pull_request === "number" ? params.pull_request : Number.NaN;
			if (!Number.isInteger(pullRequest) || pullRequest < 1) {
				return { content: text("pull_request must be a positive integer"), isError: true };
			}
			const repo = resolveRepo(mode, params);
			if (!repo) {
				return { content: text("no repository: pass repo as owner/name, or select a queue item first"), isError: true };
			}

			const diff = await fetchDiff(repo, pullRequest, {
				...mode.tokenOptions(),
				maxPatchFiles: typeof params.max_files === "number" ? params.max_files : undefined,
			});
			return {
				content: text(diffToText(diff)),
				details: {
					repo,
					pull_request: pullRequest,
					files: diff.files.map(({ path, status, additions, deletions }) => ({ path, status, additions, deletions })),
					total_files: diff.totalFiles,
					additions: diff.additions,
					deletions: diff.deletions,
					truncated: diff.truncated,
					error: diff.error ?? null,
				},
				isError: Boolean(diff.error),
			};
		},
	});

	pi.registerTool({
		name: "bluefin_review_trace",
		label: "Review Trace",
		description:
			"Render the recorded pipeline trace for one pull request from the appliance's durable state: run state machine, review batch events, landing lifecycle, and receipt findings.",
		parameters: z.object({
			pull_request: z.number().describe("Pull request number"),
			repo: z.string().describe("owner/repo; defaults to the selected item's repository").optional(),
		}),
		async execute(_id, params) {
			const pullRequest = typeof params.pull_request === "number" ? params.pull_request : Number.NaN;
			if (!Number.isInteger(pullRequest) || pullRequest < 1) {
				return { content: text("pull_request must be a positive integer"), isError: true };
			}
			const repo = resolveRepo(mode, params);
			if (!repo) {
				return { content: text("no repository: pass repo as owner/name, or select a queue item first"), isError: true };
			}

			mode.refreshState();
			const now = Date.now();
			const key = queueKey(repo, pullRequest);
			const item = mode.items.find((entry) => entry.repo === repo && entry.id === pullRequest);
			const spans = mode.tracePipeline(key, item?.title ?? "", now);
			const receipt = mode.snapshot.receipts.get(key);

			return {
				content: text(traceToText(spans, now)),
				details: {
					repo,
					pull_request: pullRequest,
					state_root: mode.stateRoot,
					receipt: receipt ?? null,
					has_state: spans.some((span) => (span.children?.length ?? 0) > 0),
				},
			};
		},
	});
	pi.registerTool({
		name: "bluefin_hive_lookup",
		label: "Hive Lookup",
		description:
			"Programmatically query the authenticated Project Bluefin Hive hub: live status, connected contributors, current contributor/task state, queue items, or the curated Markdown knowledge base.",
		parameters: z.object({
			target: z.string().describe("Target query: 'status' (default), 'knowledge' (curated patterns & test gaps), 'me' (contributor identity and task), 'queue' (ordered tasks), 'triage' (stages), or 'leaderboard' (top 25 contributors & task counts)").optional(),
		}),
		async execute(_id, params) {
			await whenReady();
			const target = typeof params.target === "string" ? params.target.toLowerCase().trim() : "status";
			const hive = mode.hive;
			if (!hive.configured) {
				return {
					content: text("Hive hub is not configured in this environment (no HIVE_HUB or contributor.env)"),
					details: { configured: false, online: false },
				};
			}

			if (target === "knowledge") {
				const knowledge = await mode.getHiveKnowledge();
				if (!knowledge) {
					return {
						content: text(`Hive knowledge export unavailable from ${hive.hub}`),
						details: { target: "knowledge", error: "unavailable" },
						isError: true,
					};
				}
				return {
					content: text(knowledge),
					details: { target: "knowledge", bytes: knowledge.length },
				};
			}

			if (target === "me") {
				const me = await fetchHiveMe({ env: process.env });
				if (!me) {
					return {
						content: text(`Hive /api/v1/me unavailable from ${hive.hub}`),
						details: { target: "me", error: "unavailable" },
						isError: true,
					};
				}
				return {
					content: text(JSON.stringify(me, null, 2)),
					details: { target: "me", data: me },
				};
			}

			if (target === "queue") {
				const lines = hive.items.map((it, idx) => `${idx + 1}. ${it.key}: ${it.title} (${it.url})`);
				return {
					content: text(lines.length > 0 ? lines.join("\n") : "Hive queue is empty"),
					details: { target: "queue", count: hive.items.length, items: hive.items },
				};
			}

			if (target === "triage") {
				const lines = hive.triage.map((g) => `${g.label} (${g.level}): ${g.count} items`);
				return {
					content: text(lines.length > 0 ? lines.join("\n") : "No triage groups recorded"),
					details: { target: "triage", groups: hive.triage },
				};
			}
			if (target === "leaderboard") {
				const roster = await fetchHiveLeaderboard({ env: process.env });
				if (roster.length === 0) {
					return {
						content: text("Hive leaderboard is currently empty or unavailable"),
						details: { target: "leaderboard", count: 0, items: [] },
					};
				}
				const lines = roster.slice(0, 25).map((c, idx) => {
					const tier = getTierInfo(c.totalTasksCompleted);
					return `${idx + 1}. @${c.username} [${c.trustTier}] — ${c.totalTasksCompleted} tasks (${c.totalTasksCompletedWithPr} with PR) | ${tier.description}`;
				});
				return {
					content: text(lines.join("\n")),
					details: { target: "leaderboard", count: roster.length, items: roster.slice(0, 25) },
				};
			}
			// Default: status overview
			const lines = [
				`hub: ${hive.hub} (${hive.online ? "online" : "offline"})`,
				`actionable_items: ${hive.actionableItems ?? "unknown"}`,
				`queue_ranked: ${mode.hiveRankedCount()} items in current scope`,
				`triage_groups: ${hive.triage.map((g) => `${g.label}:${g.count}`).join(" ")}`,
			];
			if (hive.error) lines.push(`error: ${hive.error}`);
			return {
				content: text(lines.join("\n")),
				details: {
					target: "status",
					hub: hive.hub,
					online: hive.online,
					actionable_items: hive.actionableItems ?? null,
					ranked: mode.hiveRankedCount(),
					triage: hive.triage,
					error: hive.error ?? null,
				},
			};
		},
	});
}
