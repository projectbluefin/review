/**
 * PR Reader widget model, LRU cache, content sanitizer, and navigation helper.
 * Issue #547: PR Reader in maintainer review mode.
 */

export interface PrComment {
	author: string;
	body: string;
	createdAt: string;
}

export interface PrReview {
	author: string;
	state: string;
	body?: string;
}

export interface PrDetail {
	repo: string;
	number: number;
	headSha: string;
	title: string;
	body: string;
	author: string;
	comments: PrComment[];
	reviews: PrReview[];
}

export interface ReaderState {
	activePrKey?: string;
	scrollOffset: number;
	commentDrafts: Record<string, string>;
	mode: "reading" | "composing";
}

/**
 * One conversation comment on an issue, for the native issue reader (issue #611).
 * Mirrors ``PrComment`` so the issue conversation renders through the same
 * sanitizer and formatting the PR reader already uses.
 */
export interface IssueComment {
	author: string;
	body: string;
	createdAt: string;
}

/**
 * A pull request linked to an issue through a cross-referenced timeline event.
 * The issue reader shows these without pretending an issue has a PR head, review
 * decision, or CI state.
 */
export interface LinkedPullRequest {
	number: number;
	title: string;
	state: string;
	url: string;
}

/**
 * The native reading surface of one issue, for issue #611. Populated through
 * ``fetchIssueDetail`` from the issue, comments, and timeline endpoints only.
 */
export interface IssueDetail {
	repo: string;
	number: number;
	title: string;
	body: string;
	author: string;
	state: string;
	labels: string[];
	comments: IssueComment[];
	linkedPullRequests: LinkedPullRequest[];
	url: string;
}

/**
 * Strips terminal ANSI escape sequences and script tags from untrusted remote markdown.
 */
export function sanitizeMarkdown(raw: string): string {
	if (!raw) {
		return "";
	}

	// 1. Strip ANSI escape sequences:
	// Matches ESC [ ... final byte or OSC ESC ] ... ESC \ or BEL
	const ansiRegex =
		/[\u001B\u009B][[\]()#;?]*(?:(?:(?:(?:;[-a-zA-Z\d\/#&.:=?%@~_]+)*|[a-zA-Z\d]+(?:;[-a-zA-Z\d\/#&.:=?%@~_]*)*)?\u0007)|(?:(?:\d{1,4}(?:;\d{0,4})*)?[\dA-PR-TZcf-ntqry=><~]))/g;

	let cleaned = raw.replace(ansiRegex, "");

	// Also catch any standalone escape characters if left
	cleaned = cleaned.replace(/\u001B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])/g, "");

	// 2. Strip <script...>...</script> tags and unclosed / self-closing <script...> tags
	// Case-insensitive, multiline/dotAll
	cleaned = cleaned.replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, "");
	cleaned = cleaned.replace(/<script\b[^>]*\/?>/gi, "");

	return cleaned;
}

/**
 * LRU cache bounded to maxEntries for PR details.
 * Cache key: `${repo}#${prNumber}@${headSha}`.
 */
export class PrDetailCache<T = PrDetail> {
	private readonly maxEntries: number;
	private readonly map = new Map<string, T>();

	constructor(maxEntries = 50) {
		this.maxEntries = maxEntries > 0 ? maxEntries : 50;
	}

	get(key: string): T | undefined {
		const entry = this.map.get(key);
		if (entry === undefined) {
			return undefined;
		}
		// Refresh LRU order: delete and re-insert
		this.map.delete(key);
		this.map.set(key, entry);
		return entry;
	}

	set(key: string, detail: T): void {
		if (this.map.has(key)) {
			this.map.delete(key);
		} else if (this.map.size >= this.maxEntries) {
			// Evict oldest item (first key in map iterator)
			const oldestKey = this.map.keys().next().value;
			if (oldestKey !== undefined) {
				this.map.delete(oldestKey);
			}
		}
		this.map.set(key, detail);
	}

	has(key: string): boolean {
		return this.map.has(key);
	}

	clear(): void {
		this.map.clear();
	}

	size(): number {
		return this.map.size;
	}
}

/**
 * Render a cached PR detail as readable lines for the reader pane (issue #547).
 * The body and each conversation comment / review summary are sanitized so
 * remote content cannot inject terminal controls, HTML, or shell through the
 * viewer. Headings and block text become plain lines; the data still reads.
 */
export function prDetailToLines(detail: PrDetail | undefined): string[] {
	if (!detail) return ["(no PR selected)"];
	const lines: string[] = [];
	const inline = (value: string): string => sanitizeMarkdown(value).replace(/[\r\n]+/g, " ").trim();
	const body = sanitizeMarkdown(detail.body);
	if (body) {
		lines.push(...body.split("\n"));
	} else {
		lines.push("_(no description)_");
	}
	if (detail.comments.length > 0) {
		lines.push("", "── Conversation ──", "");
		for (const comment of detail.comments) {
			const author = inline(comment.author);
			const who = author ? `@${author}` : "?";
			const stamp = comment.createdAt ? ` · ${inline(comment.createdAt)}` : "";
			lines.push(`${who}${stamp}`);
			const bodyText = sanitizeMarkdown(comment.body);
			lines.push(bodyText ? bodyText : "_(comment)_");
			lines.push("");
		}
	} else {
		lines.push("", "── No comments yet ──");
	}
	if (detail.reviews.length > 0) {
		lines.push("── Reviews ──", "");
		for (const review of detail.reviews) {
			const author = inline(review.author);
			const who = author ? `@${author}` : "?";
			const state = inline(review.state) || "unknown";
			lines.push(`[${state}] ${who}`);
			if (review.body) {
				const bodyText = sanitizeMarkdown(review.body);
				lines.push(bodyText ? bodyText : "_(no body)_");
				lines.push("");
			}
		}
	}
	return lines;
}

export function issueDetailToLines(detail: IssueDetail | undefined): string[] {
	if (!detail) return ["(no issue selected)"];
	const lines: string[] = [];
	const inline = (value: string): string => sanitizeMarkdown(value).replace(/[\r\n]+/g, " ").trim();
	const body = sanitizeMarkdown(detail.body);
	if (body) {
		lines.push(...body.split("\n"));
	} else {
		lines.push("_(no description)_");
	}
	if (detail.comments.length > 0) {
		lines.push("", "── Conversation ──", "");
		for (const comment of detail.comments) {
			const author = inline(comment.author);
			const who = author ? `@${author}` : "?";
			const stamp = comment.createdAt ? ` · ${inline(comment.createdAt)}` : "";
			lines.push(`${who}${stamp}`);
			const bodyText = sanitizeMarkdown(comment.body);
			lines.push(bodyText ? bodyText : "_(comment)_");
			lines.push("");
		}
	} else {
		lines.push("", "── No comments yet ──");
	}
	if (detail.linkedPullRequests.length > 0) {
		lines.push("", "── Linked pull requests ──", "");
		for (const pr of detail.linkedPullRequests) {
			const state = inline(pr.state) || "unknown";
			const title = inline(pr.title) || "_(untitled)_";
			lines.push(`#${pr.number} [${state}] ${title}`);
		}
	}
	return lines;
}

/**
 * Navigation helper across filtered PR keys.
 * Bounded or wrapped navigation across filtered PR keys.
 */
export function getNextPrKey(
	keys: string[],
	currentKey: string,
	direction: "next" | "prev",
): string {
	if (!keys || keys.length === 0) {
		return currentKey;
	}

	const currentIndex = keys.indexOf(currentKey);
	if (currentIndex === -1) {
		// If currentKey is not in keys list, return first item for next, last item for prev
		return direction === "next" ? keys[0] : keys[keys.length - 1];
	}

	if (direction === "next") {
		const nextIndex = (currentIndex + 1) % keys.length;
		return keys[nextIndex];
	} else {
		const prevIndex = (currentIndex - 1 + keys.length) % keys.length;
		return keys[prevIndex];
	}
}
