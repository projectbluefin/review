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
import type { IssueComment, IssueDetail, LinkedPullRequest, PrComment, PrDetail, PrReview } from "./reader.ts";

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
	/** Exact head of the pull request at queue-read time. */
	headSha?: string;
	/** GitHub accepted this pull request into its auto-merge lifecycle. */
	autoMergeEnabled?: boolean;
	/** `owner/repo#number` of every issue this pull request closes. */
	closingIssues?: string[];
	/** `owner/repo#number` of merged PRs that reference or close this issue. */
	closedByPrs?: string[];
	/** `owner/repo#number` of open or merged pull requests submitted for this issue. */
	submittedPrs?: string[];
	/** Changed workflow files reported by GitHub for exclusion from slay/review. */
	workflowFiles?: string[];
	/** Whether GitHub returned the complete changed-file list. */
	changedFilesComplete?: boolean;
}

export interface QueueResult {
	items: QueueItem[];
	error?: string;
	/** The caller canceled this request; it is not a queue failure. */
	cancelled?: boolean;
	fetchedAt: number;
	/** More open items exist than `limit` allowed; the queue is a prefix. */
	truncated?: boolean;
	/** Authenticated GitHub login that produced this queue read. */
	viewerLogin?: string;
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
	headRefOid
	files(first: 100) {
		pageInfo { hasNextPage }
		nodes { path }
	}
	autoMergeRequest { enabledAt }
	commits(last: 1) {
		nodes {
			commit {
				statusCheckRollup { state }
				checkSuites(first: 50) {
					pageInfo { hasNextPage }
					nodes { status conclusion }
				}
			}
		}
	}
	closingIssuesReferences(first: 5) {
		nodes { number repository { nameWithOwner } }
	}
`;

/** What an issue carries beyond the shared queue fields. */
const ISSUE_ITEM_FIELDS = `
	closedByPullRequestsReferences(first: 5) {
		nodes {
			number
			state
			merged
			repository { nameWithOwner }
		}
	}
`;

export const PR_QUEUE_QUERY = `
query($search: String!, $cursor: String) {
	viewer { login }
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
	viewer { login }
	search(query: $search, type: ISSUE, first: 50, after: $cursor) {
		pageInfo { hasNextPage endCursor }
		nodes {
			... on Issue {
				${QUEUE_FIELDS}
				${ISSUE_ITEM_FIELDS}
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
	autoMergeRequest?: { enabledAt?: string } | null;
	author?: { login?: string } | null;
	repository?: { nameWithOwner?: string } | null;
	labels?: { nodes?: Array<{ name?: string }> } | null;
	files?: { pageInfo?: { hasNextPage?: boolean }; nodes?: Array<{ path?: string }> } | null;
	commits?: {
		nodes?: Array<{
			commit?: {
				statusCheckRollup?: { state?: string } | null;
				checkSuites?: {
					pageInfo?: { hasNextPage?: boolean };
					nodes?: Array<{ status?: string; conclusion?: string | null }>;
				} | null;
			};
		}>;
	} | null;
	closingIssuesReferences?: { nodes?: Array<{ number?: number; repository?: { nameWithOwner?: string } | null }> } | null;
	closedByPullRequestsReferences?: {
		nodes?: Array<{
			number?: number;
			state?: string;
			merged?: boolean;
			repository?: { nameWithOwner?: string } | null;
		}>;
	} | null;
}

export function toCiStatus(
	state?: string,
	checkSuites?: { pageInfo?: { hasNextPage?: boolean }; nodes?: Array<{ status?: string; conclusion?: string | null }> } | null,
): CiStatus | undefined {
	const rollup = state?.trim().toUpperCase();
	if (rollup === "SUCCESS") return "success";
	if (rollup === "FAILURE" || rollup === "ERROR") return "failure";
	if (rollup) return "pending";

	const suites = checkSuites?.nodes ?? [];
	const failedSuite = suites.some((suite) => {
		if (suite.status?.toUpperCase() !== "COMPLETED") return false;
		const conclusion = suite.conclusion?.toUpperCase();
		return conclusion !== undefined && !["SUCCESS", "NEUTRAL", "SKIPPED"].includes(conclusion);
	});
	if (failedSuite) return "failure";
	const pendingSuite = checkSuites?.pageInfo?.hasNextPage === true
		|| suites.some((suite) => suite.status?.toUpperCase() !== "COMPLETED" || !suite.conclusion);
	if (pendingSuite) return "pending";
	if (suites.length > 0) return "success";
	return undefined;
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
	if (typeof node.number !== "number" || !node.repository?.nameWithOwner) return undefined;
	const updated = node.updatedAt ? Date.parse(node.updatedAt) : Number.NaN;
	return {
		id: node.number,
		type: mode === "prs" ? "pr" : "issue",
		repo: node.repository.nameWithOwner,
		title: node.title ?? "(untitled)",
		author: node.author?.login ?? "unknown",
		url: node.url ?? "",
		updatedAt: Number.isNaN(updated) ? 0 : updated,
		draft: node.isDraft === true,
		ciStatus: toCiStatus(
			node.commits?.nodes?.[0]?.commit?.statusCheckRollup?.state,
			node.commits?.nodes?.[0]?.commit?.checkSuites,
		),
		mergeState: toMergeState(node.mergeable),
		reviewState: toReviewState(node.reviewDecision),
		labels: (node.labels?.nodes ?? []).map((label) => label.name ?? "").filter(Boolean),
		additions: node.additions,
		deletions: node.deletions,
		changedFiles: node.changedFiles,
		headSha: node.headRefOid,
		autoMergeEnabled: Boolean(node.autoMergeRequest?.enabledAt),
		workflowFiles:
			mode === "prs"
				? (node.files?.nodes ?? []).map((file) => file.path ?? "").filter((path) => path.startsWith(".github/workflows/"))
				: undefined,
		changedFilesComplete:
			mode === "prs" && node.files
				? node.files.pageInfo?.hasNextPage !== true
					&& (node.changedFiles === undefined || (node.files.nodes ?? []).length >= node.changedFiles)
				: undefined,
		closingIssues: (node.closingIssuesReferences?.nodes ?? [])
			.map((reference) =>
				reference.repository?.nameWithOwner && typeof reference.number === "number"
					? `${reference.repository.nameWithOwner}#${reference.number}`
					: "",
			)
			.filter(Boolean),
		closedByPrs: (node.closedByPullRequestsReferences?.nodes ?? [])
			.filter((pr) => pr.merged === true || pr.state?.toUpperCase() === "MERGED")
			.map((pr) =>
				pr.repository?.nameWithOwner && typeof pr.number === "number"
					? `${pr.repository.nameWithOwner}#${pr.number}`
					: "",
			)
			.filter(Boolean),
		submittedPrs: (node.closedByPullRequestsReferences?.nodes ?? [])
			.filter((pr) => pr.merged === true || ["OPEN", "MERGED"].includes(pr.state?.toUpperCase() ?? ""))
			.map((pr) =>
				pr.repository?.nameWithOwner && typeof pr.number === "number"
					? `${pr.repository.nameWithOwner}#${pr.number}`
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
	let viewerLogin: string | undefined;
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
				data?: {
					viewer?: { login?: string };
					search?: { nodes?: SearchNode[]; pageInfo?: { hasNextPage?: boolean; endCursor?: string } };
				};
				errors?: Array<{ message?: string }>;
			};
			viewerLogin ??= payload.data?.viewer?.login;
			if (payload.errors?.length) {
				return { items, error: payload.errors.map((e) => e.message ?? "unknown").join("; "), fetchedAt: Date.now() };
			}
			for (const node of payload.data?.search?.nodes ?? []) {
				const item = toQueueItem(node, mode);
				if (item) items.push(item);
			}
			const pageInfo = payload.data?.search?.pageInfo;
			if (!pageInfo?.hasNextPage || !pageInfo.endCursor) {
				return { items: items.slice(0, limit), fetchedAt: Date.now(), truncated: items.length > limit, viewerLogin };
			}
			cursor = pageInfo.endCursor;
		}
		// Stopped on the ceiling rather than the end of the queue: say so, so the
		// counter cannot read as "this is everything open".
		return { items: items.slice(0, limit), fetchedAt: Date.now(), truncated: true, viewerLogin };
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
				viewerLogin,
			};
		}
		return { items, error: error instanceof Error ? error.message : String(error), fetchedAt: Date.now(), viewerLogin };
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
	const extras = mode === "prs" ? PR_ITEM_FIELDS : ISSUE_ITEM_FIELDS;
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

export interface IssueAdmissionTarget {
	owner: string;
	repo: string;
	number: number;
}

export interface AdmittedIssue {
	owner: string;
	repo: string;
	number: number;
	closed: boolean;
	labels: string[];
	labelsTruncated: boolean;
}

export interface IssueAdmissionResult {
	issues: AdmittedIssue[];
	error?: string;
}

/**
 * Fail-closed fresh admission read for issues.
 *
 * Queries GitHub GraphQL by repository and issue number.
 * Unlike fetchItemsByKey:
 * - Absence of a requested issue node fails the whole read.
 * - Partial GraphQL errors fail the whole read.
 * - Incomplete label evidence (labels page hasNextPage: true) is recorded.
 * - Returned repository and issue identities must match exactly.
 */
export async function fetchIssueAdmission(
	targets: readonly IssueAdmissionTarget[],
	options: FetchOptions = {},
): Promise<IssueAdmissionResult> {
	const { token, signal } = options;
	const doFetch = options.fetchImpl ?? fetch;
	if (targets.length === 0) return { issues: [] };
	if (!token) {
		return { issues: [], error: "no GitHub credential (set GH_TOKEN or run gh auth login)" };
	}

	const targetList = targets.map((t, idx) => ({
		alias: `iss${idx}`,
		owner: t.owner,
		repo: t.repo,
		number: t.number,
	}));

	const query = `query {\n${targetList
		.map(
			({ alias, owner, repo, number }) =>
				`\t${alias}: repository(owner: ${JSON.stringify(owner)}, name: ${JSON.stringify(repo)}) {\n` +
				`\t\tnameWithOwner\n` +
				`\t\tissue(number: ${number}) {\n` +
				`\t\t\tnumber\n` +
				`\t\t\tclosed\n` +
				`\t\t\trepository { nameWithOwner }\n` +
				`\t\t\tlabels(first: 100) {\n` +
				`\t\t\t\tpageInfo { hasNextPage }\n` +
				`\t\t\t\tnodes { name }\n` +
				`\t\t\t}\n` +
				`\t\t}\n` +
				`\t}`,
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
			return { issues: [], error: `GitHub GraphQL ${response.status} ${response.statusText}` };
		}
		const payload = (await response.json()) as {
			data?: Record<
				string,
				{
					nameWithOwner?: string;
					issue?: {
						number?: number;
						closed?: boolean;
						repository?: { nameWithOwner?: string };
						labels?: { pageInfo?: { hasNextPage?: boolean }; nodes?: Array<{ name?: string }> };
					} | null;
				} | null
			>;
			errors?: Array<{ message?: string }>;
		};

		if (payload.errors?.length) {
			return {
				issues: [],
				error: payload.errors.map((e) => e.message ?? "unknown").join("; "),
			};
		}

		const issues: AdmittedIssue[] = [];
		for (const t of targetList) {
			const repoNode = payload.data?.[t.alias];
			if (!repoNode) {
				return { issues: [], error: `repository ${t.owner}/${t.repo} not found` };
			}
			const issueNode = repoNode.issue;
			if (!issueNode) {
				return { issues: [], error: `issue ${t.owner}/${t.repo}#${t.number} not found` };
			}
			const returnedRepo = issueNode.repository?.nameWithOwner ?? repoNode.nameWithOwner;
			const expectedRepo = `${t.owner}/${t.repo}`;
			if (returnedRepo !== expectedRepo) {
				return {
					issues: [],
					error: `repository mismatch for issue ${expectedRepo}#${t.number}: got ${returnedRepo}`,
				};
			}
			if (typeof issueNode.number !== "number" || issueNode.number !== t.number) {
				return {
					issues: [],
					error: `issue number mismatch: expected #${t.number}, got #${issueNode.number}`,
				};
			}
			const labels = (issueNode.labels?.nodes ?? []).map((l) => l.name ?? "").filter(Boolean);
			// Missing freshness fields are unreadable evidence and must fail closed.
			const labelsTruncated = issueNode.labels?.pageInfo?.hasNextPage !== false;
			issues.push({
				owner: t.owner,
				repo: t.repo,
				number: t.number,
				closed: issueNode.closed !== false,
				labels,
				labelsTruncated,
			});
		}
		return { issues };
	} catch (error) {
		if (signal?.aborted) return { issues: [], error: "aborted" };
		return { issues: [], error: error instanceof Error ? error.message : String(error) };
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
	/**
	 * Exact head SHA the diff was fetched against. A remote diff proves only what
	 * GitHub says, never what is on disk, so executable verification must
	 * materialize this exact head and refuse on any mismatch (projectbluefin
	 * /review#471). Null when the head cannot be resolved: the files are still
	 * returned, but verification must refuse until an exact head exists.
	 */
	headSha: string | null;
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
		result.headSha = await fetchPrHeadSha(repo, pullRequest, { token, signal, fetchImpl: options.fetchImpl });
		return result;
	} catch (error) {
		result.error = error instanceof Error ? error.message : String(error);
		return result;
	}
}

/**
 * The pull request's head commit SHA, read from the PR itself rather than the
 * file list. A diff of files says nothing about which commit is on disk, so the
 * head is fetched from the PR that owns them.
 */
async function fetchPrHeadSha(
	repo: string,
	pullRequest: number,
	options: FetchOptions = {},
): Promise<string | null> {
	const doFetch = options.fetchImpl ?? fetch;
	try {
		const response = await doFetch(
			`https://api.github.com/repos/${repo}/pulls/${pullRequest}`,
			{ headers: headers(options.token), signal: options.signal, redirect: "error" },
		);
		if (!response.ok) return null;
		const payload = (await response.json()) as { head?: { sha?: string | null } | null } | null;
		return payload?.head?.sha ?? null;
	} catch {
		// An unreadable PR must not sink the diff: the files are still returned and
		// the verification gate refuses when headSha is null.
		return null;
	}
}

/**
 * Exact-head verification gate for executable verification. Execution may only
 * proceed when an exact head was expected and the materialized workspace is that
 * very head. A missing expected head, an unreadable workspace, or any mismatch
 * all refuse: a fetched diff does not prove the workspace contains that head
 * (projectbluefin/review#471). Pure and side-effect free so the gate is testable
 * in isolation from the worktree materialization that produces `workspaceSha`.
 */
export function exactHeadVerified(expectedSha: string | null | undefined, workspaceSha: string | null | undefined): boolean {
	return typeof expectedSha === "string" && expectedSha === workspaceSha;
}

export interface CollaboratorPermissionResult {
	permission?: string;
	isCollaborator: boolean;
	error?: string;
}

/**
 * Query authenticated user repository permission from GitHub REST API:
 * GET /repos/{owner}/{repo}/collaborators/{username}/permission
 *
 * Returns the authoritative GitHub permission (e.g. "admin", "write", "read", "none").
 */
export async function fetchCollaboratorPermission(
	repo: string,
	username: string,
	options: FetchOptions = {},
): Promise<CollaboratorPermissionResult> {
	const { token, signal } = options;
	const doFetch = options.fetchImpl ?? fetch;
	try {
		const response = await doFetch(
			`https://api.github.com/repos/${repo}/collaborators/${username}/permission`,
			{ headers: headers(token), signal, redirect: "error" },
		);
		if (!response.ok) {
			if (response.status === 404) {
				return { permission: "none", isCollaborator: false };
			}
			return { isCollaborator: false, error: `GitHub REST ${response.status} ${response.statusText}` };
		}
		const data = (await response.json()) as { permission?: string; role_name?: string };
		const perm = data.permission ?? data.role_name ?? "none";
		return {
			permission: perm,
			isCollaborator: perm === "admin" || perm === "write" || perm === "maintain",
		};
	} catch (error) {
		return {
			isCollaborator: false,
			error: error instanceof Error ? error.message : String(error),
		};
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

/** The empty conversation a PR without comments or reviews renders from. */
const NO_COMMENTS: PrComment[] = [];
const NO_REVIEWS: PrReview[] = [];

/** Map the raw REST payloads for a pull request into a `PrDetail` (issue #547). */
export function parsePrDetail(
	repo: string,
	number: number,
	headSha: string,
	pull: Partial<{
		title?: string;
		body?: string | null;
		user?: { login?: string } | null;
		head?: { sha?: string };
	}>,
	comments: ReadonlyArray<Partial<{ user?: { login?: string } | null; created_at?: string; body?: string | null }>> = [],
	reviews: ReadonlyArray<Partial<{ user?: { login?: string } | null; state?: string; body?: string | null }>> = [],
): PrDetail {
	const conversation: PrComment[] = comments.slice(0, 50).map((comment) => ({
		author: comment.user?.login || "?",
		body: comment.body ?? "",
		createdAt: comment.created_at ?? "",
	}));
	const reviewList: PrReview[] = reviews.slice(0, 50).map((review) => ({
		author: review.user?.login || "?",
		state: review.state ?? "unknown",
		body: review.body ?? undefined,
	}));
	return {
		repo,
		number,
		headSha: headSha || (pull.head?.sha ?? ""),
		title: pull.title ?? "(untitled)",
		body: pull.body ?? "",
		author: pull.user?.login ?? "unknown",
		comments: conversation.length > 0 ? conversation : NO_COMMENTS,
		reviews: reviewList.length > 0 ? reviewList : NO_REVIEWS,
	};
}

export interface PrDetailResult {
	detail?: PrDetail;
	error?: string;
	cancelled?: boolean;
}

/**
 * Fetch the reading surface of one pull request: its body, conversation
 * comments, and top-level reviews, for the PR Reader (issue #547).
 *
 * Three bounded REST reads are fanned out so the body and the conversation
 * arrive together; any failing read fails the detail rather than rendering a
 * half-truth on a maintainer's screen. The dashboard serves this through its
 * PR-detail cache, so re-reading a pinned pull request is a cache hit and this
 * network only happens once per ``headSha``.
 */
export async function fetchPrDetail(
	repo: string,
	pullRequest: number,
	options: FetchOptions = {},
): Promise<PrDetailResult> {
	const { token, signal } = options;
	const doFetch = options.fetchImpl ?? fetch;
	if (!token) {
		return { detail: undefined, error: "no GitHub credential (set GH_TOKEN or run gh auth login)" };
	}
	const deadline = deadlineSignal(options.timeoutMs ?? 15_000, signal);
	const readJson = async <T>(url: string): Promise<{ payload?: T; error?: string }> => {
		try {
			const response = await doFetch(url, { headers: headers(token), signal: deadline, redirect: "error" });
			if (!response.ok) {
				return { error: `GitHub REST ${response.status} ${response.statusText}` };
			}
			return { payload: (await response.json()) as T };
		} catch (error) {
			if (signal?.aborted) return { error: "cancelled" };
			return { error: error instanceof Error ? error.message : String(error) };
		}
	};

	const base = `https://api.github.com/repos/${repo}`;
	const [pull, comments, reviews] = await Promise.all([
		readJson<{ title?: string; body?: string | null; user?: { login?: string } | null; head?: { sha?: string } }>(
			`${base}/pulls/${pullRequest}`,
		),
		readJson<Array<{ user?: { login?: string } | null; created_at?: string; body?: string | null }>>(
			`${base}/issues/${pullRequest}/comments?per_page=100`,
		),
		readJson<Array<{ user?: { login?: string } | null; state?: string; body?: string | null }>>(
			`${base}/pulls/${pullRequest}/reviews?per_page=100`,
		),
	]);

	const firstError = [pull.error, comments.error, reviews.error].find(Boolean);
	if (firstError) {
		return { detail: undefined, error: firstError };
	}
	if (comments.payload && !Array.isArray(comments.payload)) {
		return { detail: undefined, error: "GitHub returned malformed comments payload" };
	}
	if (reviews.payload && !Array.isArray(reviews.payload)) {
		return { detail: undefined, error: "GitHub returned malformed reviews payload" };
	}
	return {
		detail: parsePrDetail(
			repo,
			pullRequest,
			"",
			pull.payload ?? {},
			comments.payload,
			reviews.payload,
		),
	};
}

interface IssuePayload {
	title?: string;
	body?: string | null;
	user?: { login?: string } | null;
	state?: string;
	labels?: Array<{ name?: string }> | null;
	url?: string;
}

interface CommentPayload {
	user?: { login?: string } | null;
	created_at?: string;
	body?: string | null;
}

/** One cross-referenced timeline event naming a pull request linked to the issue. */
interface TimelineIssue {
	number?: number;
	title?: string;
	state?: string;
	url?: string;
	pull_request?: unknown;
}

interface TimelineEvent {
	event?: string;
	source?: { issue?: TimelineIssue };
}

/**
 * The native reading surface of one issue (issue #611).
 *
 * Populated from the issue, its conversation comments, and its timeline. The
 * timeline backs only the linked-pull-request list; the issue reader never
 * calls the PR diff/files endpoint and never starts an agent turn.
 */
export function parseIssueDetail(
	repo: string,
	number: number,
	issue: IssuePayload = {},
	comments: ReadonlyArray<CommentPayload> = [],
	timelineEvents: ReadonlyArray<TimelineEvent> = [],
): IssueDetail {
	const conversation: IssueComment[] = comments.slice(0, 50).map((comment) => ({
		author: comment.user?.login || "?",
		body: comment.body ?? "",
		createdAt: comment.created_at ?? "",
	}));
	const seen = new Set<number>();
	const linked: LinkedPullRequest[] = [];
	for (const event of timelineEvents) {
		const source = event?.source?.issue;
		// A cross-referenced event names the pull request that links to this issue.
		if (event?.event !== "cross_referenced" || !source || !source.pull_request) continue;
		const prNumber = source.number;
		if (typeof prNumber !== "number" || seen.has(prNumber)) continue;
		seen.add(prNumber);
		linked.push({
			number: prNumber,
			title: source.title ?? "(untitled)",
			state: source.state ?? "unknown",
			url: source.url ?? "",
		});
	}
	const labels = (issue.labels ?? []).map((label) => label.name ?? "").filter(Boolean);
	return {
		repo,
		number,
		title: issue.title ?? "(untitled)",
		body: issue.body ?? "",
		author: issue.user?.login ?? "unknown",
		state: issue.state ?? "unknown",
		labels,
		comments: conversation,
		linkedPullRequests: linked,
		url: issue.url ?? "",
	};
}

export interface IssueDetailResult {
	detail?: IssueDetail;
	error?: string;
	cancelled?: boolean;
}

/**
 * Fetch the reading surface of one issue: its body, metadata, conversation
 * comments, and linked pull requests, for the native issue reader (issue #611).
 *
 * Three bounded REST reads are fanned out: the issue, its conversation, and its
 * timeline. The issue and comments are the reading surface itself, so either
 * failing read fails the detail rather than rendering a half-truth. The timeline
 * only backs the linked-PR list, so a missing or forbidden timeline yields no
 * linked PRs instead of failing the whole read. None of these touch the PR
 * diff/files endpoint, and no read starts an agent turn.
 */
export async function fetchIssueDetail(
	repo: string,
	issueNumber: number,
	options: FetchOptions = {},
): Promise<IssueDetailResult> {
	const { token, signal } = options;
	const doFetch = options.fetchImpl ?? fetch;
	if (!token) {
		return { detail: undefined, error: "no GitHub credential (set GH_TOKEN or run gh auth login)" };
	}
	const deadline = deadlineSignal(options.timeoutMs ?? 15_000, signal);
	const readJson = async <T>(url: string): Promise<{ payload?: T; error?: string }> => {
		try {
			const response = await doFetch(url, { headers: headers(token), signal: deadline, redirect: "error" });
			if (!response.ok) {
				return { error: `GitHub REST ${response.status} ${response.statusText}` };
			}
			return { payload: (await response.json()) as T };
		} catch (error) {
			if (signal?.aborted) return { error: "cancelled" };
			return { error: error instanceof Error ? error.message : String(error) };
		}
	};

	const base = `https://api.github.com/repos/${repo}`;
	const [issue, comments, timeline] = await Promise.all([
		readJson<{ title?: string; body?: string | null; user?: { login?: string } | null; state?: string; labels?: Array<{ name?: string }> | null; url?: string }>(
			`${base}/issues/${issueNumber}`,
		),
		readJson<Array<{ user?: { login?: string } | null; created_at?: string; body?: string | null }>>(
			`${base}/issues/${issueNumber}/comments?per_page=100`,
		),
		readJson<Array<Record<string, unknown>>>(
			`${base}/issues/${issueNumber}/timeline?filter=all&per_page=100`,
		),
	]);

	const coreError = [issue.error, comments.error].find(Boolean);
	if (coreError) {
		return { detail: undefined, error: coreError };
	}
	if (comments.payload && !Array.isArray(comments.payload)) {
		return { detail: undefined, error: "GitHub returned malformed comments payload" };
	}
	const linkedEvents = Array.isArray(timeline.payload) ? timeline.payload : [];
	return { detail: parseIssueDetail(repo, issueNumber, issue.payload ?? {}, comments.payload, linkedEvents) };
}
