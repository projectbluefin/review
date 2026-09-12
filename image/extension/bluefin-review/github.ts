/**
 * GitHub access for the review queue.
 *
 * Two calls matter: the org-wide queue (GraphQL search, paginated) and a bounded
 * diff for one pull request (REST files API, which returns per-file patches and
 * counts instead of one unbounded blob).
 *
 * Failures are returned, never swallowed. A queue that silently renders empty on
 * a 401 is worse than no queue: it says "no work" when it means "no credential".
 */

import { execFileSync } from "node:child_process";
import { deadlineSignal } from "./deadline.ts";

export type QueueMode = "prs" | "issues";
export type CiStatus = "success" | "failure" | "pending";
/** GitHub's mergeability, normalized to the dashboard's vocabulary. */
export type MergeState = "clean" | "dirty" | "unknown";
/** GitHub's review decision, normalized to the dashboard's vocabulary. */
export type ReviewState = "approved" | "changes_requested" | "review_required" | "unknown";

export interface QueueItem {
	id: number;
	type: "pr" | "issue";
	repo: string;
	title: string;
	author: string;
	url: string;
	updatedAt: number;
	draft: boolean;
	ciStatus?: CiStatus;
	mergeState: MergeState;
	reviewState: ReviewState;
	labels: string[];
	additions?: number;
	deletions?: number;
	changedFiles?: number;
	/** `owner/repo#number` of every issue this pull request closes. */
	closingIssues?: string[];
}

export interface QueueResult {
	items: QueueItem[];
	error?: string;
	/** The caller canceled this request; it is not a queue failure. */
	cancelled?: boolean;
	fetchedAt: number;
	/** More open items exist than `limit` allowed; the queue is a prefix. */
	truncated?: boolean;
}

export const DEFAULT_ORG = "projectbluefin";

const QUEUE_FIELDS = `
	number
	title
	url
	updatedAt
	author { login }
	repository { nameWithOwner }
	labels(first: 20) { nodes { name } }
`;

/** What a pull request carries beyond the fields an issue shares. */
const PR_ITEM_FIELDS = `
	isDraft
	mergeable
	reviewDecision
	additions
	deletions
	changedFiles
	commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
	closingIssuesReferences(first: 5) {
		nodes { number repository { nameWithOwner } }
	}
`;

export const PR_QUEUE_QUERY = `
query($search: String!, $cursor: String) {
	search(query: $search, type: ISSUE, first: 50, after: $cursor) {
		pageInfo { hasNextPage endCursor }
		nodes {
			... on PullRequest {
				${QUEUE_FIELDS}
				${PR_ITEM_FIELDS}
			}
		}
	}
}`;

export const ISSUE_QUEUE_QUERY = `
query($search: String!, $cursor: String) {
	search(query: $search, type: ISSUE, first: 50, after: $cursor) {
		pageInfo { hasNextPage endCursor }
		nodes {
			... on Issue {
				${QUEUE_FIELDS}
			}
		}
	}
}`;

/**
 * What the queue covers: a whole organization, or one `owner/repo`.
 *
 * A scope is a search qualifier, not a filter applied after the fact — asking
 * GitHub for one repository is what makes reviewing a repository outside the
 * organization possible at all.
 */
export interface QueueScope {
	kind: "org" | "repo";
	value: string;
}

export function orgScope(org: string): QueueScope {
	return { kind: "org", value: org };
}

/** Parse `owner/repo`, or a full GitHub URL, into a repository scope. */
export function parseScope(input: string, defaultOrg: string): QueueScope | undefined {
	const trimmed = input.trim().replace(/^https?:\/\/github\.com\//i, "").replace(/\.git$/, "").replace(/\/+$/, "");
	if (!trimmed) return undefined;
	if (/^[A-Za-z0-9._-]+$/.test(trimmed)) {
		// A bare name is a repository in the configured organization; an
		// organization is named with the explicit `org:` prefix below.
		return { kind: "repo", value: `${defaultOrg}/${trimmed}` };
	}
	const org = /^org:([A-Za-z0-9._-]+)$/.exec(trimmed);
	if (org) return { kind: "org", value: org[1]! };
	return /^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/.test(trimmed) ? { kind: "repo", value: trimmed } : undefined;
}

export function describeScope(scope: QueueScope): string {
	return scope.kind === "org" ? scope.value : scope.value;
}

export function searchExpression(mode: QueueMode, scope: QueueScope): string {
	const kind = mode === "prs" ? "is:pr" : "is:issue";
	const qualifier = scope.kind === "org" ? `org:${scope.value}` : `repo:${scope.value}`;
	return `${qualifier} ${kind} is:open archived:false sort:updated-desc`;
}

/**
 * Resolve a token without ever putting it on a command line.
 * `gh auth token` is the last resort and is bounded so a stalled keyring cannot
 * hang session startup.
 */
export function resolveToken(env: NodeJS.ProcessEnv = process.env): string | undefined {
	const direct = env.GITHUB_TOKEN || env.GH_TOKEN || env.COPILOT_GITHUB_TOKEN;
	if (direct) return direct;
	try {
		const token = execFileSync("gh", ["auth", "token"], { encoding: "utf8", timeout: 3000, stdio: ["ignore", "pipe", "ignore"] });
		return token.trim() || undefined;
	} catch {
		return undefined;
	}
}

function headers(token?: string): Record<string, string> {
	const value: Record<string, string> = {
		Accept: "application/vnd.github+json",
		"User-Agent": "bluefin-review-omp",
		"X-GitHub-Api-Version": "2022-11-28",
	};
	if (token) value.Authorization = `Bearer ${token}`;
	return value;
}

interface SearchNode {
	number?: number;
	title?: string;
	url?: string;
	updatedAt?: string;
	isDraft?: boolean;
	mergeable?: string | null;
	reviewDecision?: string | null;
	additions?: number;
	deletions?: number;
	closed?: boolean;
	changedFiles?: number;
	author?: { login?: string } | null;
	repository?: { nameWithOwner?: string } | null;
	labels?: { nodes?: Array<{ name?: string }> } | null;
	commits?: { nodes?: Array<{ commit?: { statusCheckRollup?: { state?: string } | null } }> } | null;
	closingIssuesReferences?: { nodes?: Array<{ number?: number; repository?: { nameWithOwner?: string } | null }> } | null;
}

function toCiStatus(state?: string): CiStatus | undefined {
	switch (state?.toUpperCase()) {
		case "SUCCESS":
			return "success";
		case "FAILURE":
		case "ERROR":
			return "failure";
		case undefined:
			return undefined;
		default:
			return "pending";
	}
}

function toMergeState(value?: string | null): MergeState {
	switch (value?.toUpperCase()) {
		case "MERGEABLE":
			return "clean";
		case "CONFLICTING":
			return "dirty";
		default:
			return "unknown";
	}
}

function toReviewState(value?: string | null): ReviewState {
	switch (value?.toUpperCase()) {
		case "APPROVED":
			return "approved";
		case "CHANGES_REQUESTED":
			return "changes_requested";
		case "REVIEW_REQUIRED":
			return "review_required";
		default:
			return "unknown";
	}
}

function toQueueItem(node: SearchNode, mode: QueueMode): QueueItem | undefined {
	if (typeof node.number !== "number") return undefined;
	const updated = node.updatedAt ? Date.parse(node.updatedAt) : Number.NaN;
	return {
		id: node.number,
		type: mode === "prs" ? "pr" : "issue",
		repo: node.repository?.nameWithOwner ?? DEFAULT_ORG,
		title: node.title ?? "(untitled)",
		author: node.author?.login ?? "ghost",
		url: node.url ?? "",
		updatedAt: Number.isNaN(updated) ? 0 : updated,
		draft: node.isDraft === true,
		ciStatus: toCiStatus(node.commits?.nodes?.[0]?.commit?.statusCheckRollup?.state),
		mergeState: toMergeState(node.mergeable),
		reviewState: toReviewState(node.reviewDecision),
		labels: (node.labels?.nodes ?? []).map((label) => label.name ?? "").filter(Boolean),
		additions: node.additions,
		deletions: node.deletions,
		changedFiles: node.changedFiles,
		closingIssues: (node.closingIssuesReferences?.nodes ?? [])
			.map((reference) =>
				reference.repository?.nameWithOwner && typeof reference.number === "number"
					? `${reference.repository.nameWithOwner}#${reference.number}`
					: "",
			)
			.filter(Boolean),
	};
}

export interface FetchOptions {
	token?: string;
	org?: string;
	/** Overrides `org`; set by the repository shortcut. */
	scope?: QueueScope;
	/** Hard ceiling on items pulled, across pages. */
	limit?: number;
	/** Ceiling on the whole walk, pages included. */
	timeoutMs?: number;
	signal?: AbortSignal;
	fetchImpl?: typeof fetch;
}

/**
 * Default queue deadline.
 *
 * An org-wide walk is three sequential GraphQL pages, each resolving check
 * rollups for fifty pull requests; measured against `projectbluefin` it takes
 * upwards of fifteen seconds, so a ceiling in that range fails the ordinary
 * case. What the ceiling is for is the pathological one: it stays under the
 * queue's own refetch cadence, so a wedged walk is abandoned and reported
 * before the next one starts rather than accumulating.
 */
export const QUEUE_TIMEOUT_MS = 45_000;

/** Fetch the open org queue, following pagination up to `limit` items. */
export async function fetchQueue(mode: QueueMode, options: FetchOptions = {}): Promise<QueueResult> {
	const { token, org = DEFAULT_ORG, limit = 150, signal } = options;
	const scope = options.scope ?? orgScope(org);
	const doFetch = options.fetchImpl ?? fetch;
	const query = mode === "prs" ? PR_QUEUE_QUERY : ISSUE_QUEUE_QUERY;
	const deadline = deadlineSignal(options.timeoutMs ?? QUEUE_TIMEOUT_MS, signal);
	const items: QueueItem[] = [];
	let cursor: string | undefined;
	if (!token) {
		if (signal?.aborted) return { items, cancelled: true, fetchedAt: Date.now() };
		return { items, error: "no GitHub credential (set GH_TOKEN or run gh auth login)", fetchedAt: Date.now() };
	}

	try {
		while (items.length < limit) {
			const response = await doFetch("https://api.github.com/graphql", {
				method: "POST",
				headers: { ...headers(token), "Content-Type": "application/json" },
				body: JSON.stringify({ query, variables: { search: searchExpression(mode, scope), cursor: cursor ?? null } }),
				signal: deadline,
				// These are API endpoints, not documents. A redirect off api.github.com
				// carries an Authorization header nowhere it belongs.
				redirect: "error",
			});
			if (!response.ok) {
				return { items, error: `GitHub GraphQL ${response.status} ${response.statusText}`, fetchedAt: Date.now() };
			}
			const payload = (await response.json()) as {
				data?: { search?: { nodes?: SearchNode[]; pageInfo?: { hasNextPage?: boolean; endCursor?: string } } };
				errors?: Array<{ message?: string }>;
			};
			if (payload.errors?.length) {
				return { items, error: payload.errors.map((e) => e.message ?? "unknown").join("; "), fetchedAt: Date.now() };
			}
			for (const node of payload.data?.search?.nodes ?? []) {
				const item = toQueueItem(node, mode);
				if (item) items.push(item);
			}
			const pageInfo = payload.data?.search?.pageInfo;
			if (!pageInfo?.hasNextPage || !pageInfo.endCursor) {
				return { items: items.slice(0, limit), fetchedAt: Date.now(), truncated: items.length > limit };
			}
			cursor = pageInfo.endCursor;
		}
		// Stopped on the ceiling rather than the end of the queue: say so, so the
		// counter cannot read as "this is everything open".
		return { items: items.slice(0, limit), fetchedAt: Date.now(), truncated: true };
	} catch (error) {
		if (signal?.aborted) return { items, cancelled: true, fetchedAt: Date.now() };
		// The deadline expired mid-walk. Keep the pages that did land: a partial
		// queue in priority order still beats an empty one, as long as it says so.
		if (deadline.aborted) {
			return {
				items,
				error: `GitHub queue timed out after ${options.timeoutMs ?? QUEUE_TIMEOUT_MS}ms`,
				fetchedAt: Date.now(),
				truncated: items.length > 0,
			};
		}
		return { items, error: error instanceof Error ? error.message : String(error), fetchedAt: Date.now() };
	}
}

/** `owner/repo#number`, as Hive names its work. */
const ITEM_KEY = /^([A-Za-z0-9._-]+\/[A-Za-z0-9._-]+)#(\d+)$/;

/**
 * Named work, fetched by identity instead of found by search.
 *
 * The queue is a search over recently updated items, and Hive's backlog is
 * mostly neither recent nor updated: measured against `projectbluefin`, six of
 * ten queued items fell outside the window, so the tool whose purpose is
 * burning that queue down never showed them. Search decides what is nearby;
 * this decides what is required.
 *
 * One aliased request for the whole set. Anything closed, moved, or of the
 * other kind is simply absent from the result — the caller reports the
 * shortfall rather than inventing a row for it.
 */
export async function fetchItemsByKey(
	keys: readonly string[],
	mode: QueueMode,
	options: FetchOptions = {},
): Promise<QueueResult> {
	const { token, signal } = options;
	const doFetch = options.fetchImpl ?? fetch;
	const items: QueueItem[] = [];
	if (keys.length === 0) return { items, fetchedAt: Date.now() };
	if (!token) {
		if (signal?.aborted) return { items, cancelled: true, fetchedAt: Date.now() };
		return { items, error: "no GitHub credential (set GH_TOKEN or run gh auth login)", fetchedAt: Date.now() };
	}

	const targets: Array<{ alias: string; owner: string; name: string; number: number }> = [];
	for (const key of keys) {
		const match = ITEM_KEY.exec(key);
		if (!match) continue;
		const [owner, name] = match[1]!.split("/") as [string, string];
		targets.push({ alias: `w${targets.length}`, owner, name, number: Number(match[2]) });
	}
	if (targets.length === 0) return { items, fetchedAt: Date.now() };

	const wanted = mode === "prs" ? "PullRequest" : "Issue";
	const extras = mode === "prs" ? PR_ITEM_FIELDS : "";
	const query = `query {\n${targets
		.map(
			({ alias, owner, name, number }) =>
				`\t${alias}: repository(owner: ${JSON.stringify(owner)}, name: ${JSON.stringify(name)}) {\n` +
				`\t\tissueOrPullRequest(number: ${number}) { ... on ${wanted} { closed ${QUEUE_FIELDS} ${extras} } }\n\t}`,
		)
		.join("\n")}\n}`;

	try {
		const response = await doFetch("https://api.github.com/graphql", {
			method: "POST",
			headers: { ...headers(token), "Content-Type": "application/json" },
			body: JSON.stringify({ query }),
			signal: deadlineSignal(options.timeoutMs ?? QUEUE_TIMEOUT_MS, signal),
			redirect: "error",
		});
		if (!response.ok) {
			return { items, error: `GitHub GraphQL ${response.status} ${response.statusText}`, fetchedAt: Date.now() };
		}
		const payload = (await response.json()) as {
			data?: Record<string, { issueOrPullRequest?: SearchNode | null } | null>;
			errors?: Array<{ message?: string }>;
		};
		// Partial data is normal here: one unreadable repository must not discard
		// the rest of Hive's queue, so errors are reported beside what did resolve.
		for (const { alias } of targets) {
			const node = payload.data?.[alias]?.issueOrPullRequest;
			// The search path is `is:open`; by name it has to be asked. Hive keeps
			// ranking work after it is closed, and a finished item is not a queue.
			if (!node || node.closed === true) continue;
			const item = toQueueItem(node, mode);
			if (item) items.push(item);
		}
		const failed = payload.errors?.length
			? payload.errors.map((entry) => entry.message ?? "unknown").join("; ")
			: undefined;
		return { items, error: failed, fetchedAt: Date.now() };
	} catch (error) {
		if (signal?.aborted) return { items, cancelled: true, fetchedAt: Date.now() };
		return { items, error: error instanceof Error ? error.message : String(error), fetchedAt: Date.now() };
	}
}

export interface DiffFile {
	path: string;
	status: string;
	additions: number;
	deletions: number;
	patch?: string;
}

export interface DiffResult {
	repo: string;
	pullRequest: number;
	files: DiffFile[];
	totalFiles: number;
	additions: number;
	deletions: number;
	truncated: boolean;
	error?: string;
}

export interface DiffOptions extends FetchOptions {
	/** Files whose patch text is included; the rest are listed with counts only. */
	maxPatchFiles?: number;
	/** Ceiling on patch characters returned, so a vendored lockfile cannot flood context. */
	maxPatchChars?: number;
}

/**
 * Bounded diff for one pull request.
 *
 * The result is honest about its bounds: `truncated` says patches were dropped,
 * and every file is still listed with its real add/delete counts.
 */
export async function fetchDiff(repo: string, pullRequest: number, options: DiffOptions = {}): Promise<DiffResult> {
	const { token, signal, maxPatchFiles = 20, maxPatchChars = 24_000 } = options;
	const doFetch = options.fetchImpl ?? fetch;
	const result: DiffResult = {
		repo,
		pullRequest,
		files: [],
		totalFiles: 0,
		additions: 0,
		deletions: 0,
		truncated: false,
	};

	try {
		const response = await doFetch(
			`https://api.github.com/repos/${repo}/pulls/${pullRequest}/files?per_page=100`,
			{ headers: headers(token), signal, redirect: "error" },
		);
		if (!response.ok) {
			result.error = `GitHub REST ${response.status} ${response.statusText}`;
			return result;
		}
		const payload = (await response.json()) as Array<{
			filename?: string;
			status?: string;
			additions?: number;
			deletions?: number;
			patch?: string;
		}>;

		let patchBudget = maxPatchChars;
		let patchedFiles = 0;
		result.totalFiles = payload.length;

		for (const file of payload) {
			const additions = file.additions ?? 0;
			const deletions = file.deletions ?? 0;
			result.additions += additions;
			result.deletions += deletions;

			let patch: string | undefined;
			if (file.patch && patchedFiles < maxPatchFiles && patchBudget > 0) {
				if (file.patch.length > patchBudget) {
					patch = `${file.patch.slice(0, patchBudget)}\n… patch truncated …`;
					result.truncated = true;
				} else {
					patch = file.patch;
				}
				patchBudget -= patch.length;
				patchedFiles += 1;
			} else if (file.patch) {
				result.truncated = true;
			}

			result.files.push({
				path: file.filename ?? "(unknown)",
				status: file.status ?? "modified",
				additions,
				deletions,
				patch,
			});
		}
		return result;
	} catch (error) {
		result.error = error instanceof Error ? error.message : String(error);
		return result;
	}
}

/** Render a diff result as the compact text an agent should read. */
export function diffToText(diff: DiffResult): string {
	if (diff.error) return `diff unavailable for ${diff.repo}#${diff.pullRequest}: ${diff.error}`;
	const lines: string[] = [
		`${diff.repo}#${diff.pullRequest}: ${diff.totalFiles} files, +${diff.additions} -${diff.deletions}${diff.truncated ? " (patches bounded)" : ""}`,
		"",
	];
	for (const file of diff.files) {
		lines.push(`${file.status} ${file.path} +${file.additions} -${file.deletions}`);
		if (file.patch) lines.push(file.patch, "");
	}
	return lines.join("\n");
}
