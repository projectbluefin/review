/**
 * Unit tests for the confirmed comment mutation path.
 *
 * `image/extension/bluefin-review/mutations.ts` is the only place in the mode
 * that turns a model's intent into an argv that writes to GitHub, and the only
 * place that re-checks a plan against live state before it runs. It had no test
 * of any kind: every guard below — argv shape, target validation, duplicate
 * rejection, and the fail-closed head-SHA revalidation — could regress silently
 * and would only be observable as a comment posted against a stale review.
 *
 * Run with:
 *   node --test tests/mutations.test.ts
 */

import assert from "node:assert/strict";
import test from "node:test";
import {
	commentInvocation,
	createCommentActionPlan,
	renderCommentActionPlan,
	validateCommentActionPlan,
	type CommentTargetSnapshot,
} from "../image/extension/bluefin-review/mutations.ts";

const PR: CommentTargetSnapshot = {
	repo: "projectbluefin/review",
	number: 42,
	type: "pull_request",
	headSha: "abc1234def5678901234567890abcdef12345678",
};

const ISSUE: CommentTargetSnapshot = {
	repo: "projectbluefin/review",
	number: 7,
	type: "issue",
};

// ── commentInvocation ────────────────────────────────────────────────────────

test("commentInvocation builds a gh pr comment argv for a pull request", () => {
	const invocation = commentInvocation(PR, "looks good");
	assert.equal(invocation.command, "gh");
	assert.deepEqual(invocation.args, [
		"pr",
		"comment",
		"42",
		"--repo",
		"projectbluefin/review",
		"--body",
		"looks good",
	]);
});

test("commentInvocation selects the issue subcommand for an issue target", () => {
	const invocation = commentInvocation(ISSUE, "triaged");
	assert.equal(invocation.args[0], "issue");
	assert.equal(invocation.args[2], "7");
});

test("commentInvocation passes the body as one argv element, never a shell string", () => {
	const body = "line one\nline two; rm -rf / $(whoami) `id`";
	const invocation = commentInvocation(PR, body);
	assert.equal(invocation.args.at(-1), body);
	// The body must not be spliced into any other argument.
	assert.equal(invocation.args.filter((arg) => arg.includes("rm -rf")).length, 1);
});

// ── createCommentActionPlan: validation ──────────────────────────────────────

test("createCommentActionPlan rejects an empty or whitespace-only body", () => {
	assert.throws(() => createCommentActionPlan([PR], ""), /body cannot be empty/i);
	assert.throws(() => createCommentActionPlan([PR], "   \n\t "), /body cannot be empty/i);
});

test("createCommentActionPlan rejects an empty target list", () => {
	assert.throws(() => createCommentActionPlan([], "hello"), /targets cannot be empty/i);
});

test("createCommentActionPlan rejects a malformed repository", () => {
	for (const repo of ["", "review", "owner/repo/extra", "owner /repo", "owner/repo;rm"]) {
		assert.throws(
			() => createCommentActionPlan([{ ...ISSUE, repo }], "hello"),
			/Invalid target repository/,
			`expected rejection for repo ${JSON.stringify(repo)}`,
		);
	}
});

test("createCommentActionPlan rejects a non-positive or non-integer number", () => {
	for (const number of [0, -1, 1.5, Number.NaN]) {
		assert.throws(
			() => createCommentActionPlan([{ ...ISSUE, number }], "hello"),
			/Invalid target number/,
			`expected rejection for number ${number}`,
		);
	}
});

test("createCommentActionPlan rejects an unknown target type", () => {
	const bogus = { ...ISSUE, type: "discussion" } as unknown as CommentTargetSnapshot;
	assert.throws(() => createCommentActionPlan([bogus], "hello"), /Invalid target type/);
});

test("createCommentActionPlan requires a head SHA on every pull request target", () => {
	const { headSha: _dropped, ...headless } = PR;
	assert.throws(
		() => createCommentActionPlan([headless as CommentTargetSnapshot], "hello"),
		/Missing pull request head/,
	);
	assert.throws(() => createCommentActionPlan([{ ...PR, headSha: "   " }], "hello"), /Missing pull request head/);
});

test("createCommentActionPlan does not require a head SHA on an issue target", () => {
	const plan = createCommentActionPlan([ISSUE], "hello");
	assert.equal(plan.targets.length, 1);
});

test("createCommentActionPlan rejects the same repo#number twice, across types", () => {
	assert.throws(
		() => createCommentActionPlan([ISSUE, { ...ISSUE }], "hello"),
		/Duplicate comment target/,
	);
	// Same key, different type: still one comment slot, still a duplicate.
	assert.throws(
		() => createCommentActionPlan([ISSUE, { ...PR, number: ISSUE.number }], "hello"),
		/Duplicate comment target/,
	);
});

test("createCommentActionPlan accepts the same number in two different repositories", () => {
	const plan = createCommentActionPlan(
		[ISSUE, { ...ISSUE, repo: "projectbluefin/common" }],
		"hello",
	);
	assert.equal(plan.targets.length, 2);
});

// ── createCommentActionPlan: plan shape ──────────────────────────────────────

test("createCommentActionPlan keeps the untrimmed body while validating the trimmed one", () => {
	const plan = createCommentActionPlan([ISSUE], "  hello  ");
	assert.equal(plan.body, "  hello  ");
});

test("createCommentActionPlan signature pins type, repo, number, and head", () => {
	const plan = createCommentActionPlan([PR], "hello");
	assert.equal(plan.signature, `comment:pull_request:projectbluefin/review#42@${PR.headSha}:hello`);
	const issuePlan = createCommentActionPlan([ISSUE], "hello");
	assert.equal(issuePlan.signature, "comment:issue:projectbluefin/review#7:hello");
});

test("createCommentActionPlan ids are stable for identical input and differ when the head moves", () => {
	const a = createCommentActionPlan([PR], "hello", 1_700_000_000_000);
	const b = createCommentActionPlan([PR], "hello", 1_700_000_000_000);
	assert.equal(a.id, b.id);

	const moved = createCommentActionPlan(
		[{ ...PR, headSha: "ffffffffffffffffffffffffffffffffffffffff" }],
		"hello",
		1_700_000_000_000,
	);
	assert.notEqual(moved.id, a.id);
	assert.notEqual(moved.signature, a.signature);

	const laterTime = createCommentActionPlan([PR], "hello", 1_700_000_000_001);
	assert.notEqual(laterTime.id, a.id);
	assert.equal(laterTime.createdAt, 1_700_000_000_001);
});

test("createCommentActionPlan freezes the plan and its target snapshots", () => {
	const targets = [{ ...PR }];
	const plan = createCommentActionPlan(targets, "hello");
	assert.equal(Object.isFrozen(plan), true);
	assert.equal(Object.isFrozen(plan.targets), true);
	assert.equal(Object.isFrozen(plan.targets[0]), true);

	// The snapshot is a copy: mutating the caller's array afterwards cannot
	// retarget an already-confirmed plan.
	targets[0] = { ...PR, repo: "attacker/repo" };
	assert.equal(plan.targets[0].repo, "projectbluefin/review");
});

// ── renderCommentActionPlan ──────────────────────────────────────────────────

test("renderCommentActionPlan shows every target, its exact argv, and the body", () => {
	const plan = createCommentActionPlan([PR, ISSUE], "ship it\nsecond line");
	const rendered = renderCommentActionPlan(plan);
	assert.match(rendered, /^Comment Action Plan \(2 targets\):/);
	assert.match(rendered, /\[pull_request\] projectbluefin\/review#42 \(head: abc1234\)/);
	assert.match(rendered, /\[issue\] projectbluefin\/review#7\n/);
	assert.match(rendered, /\$ gh pr comment 42 --repo projectbluefin\/review --body ship it/);
	assert.match(rendered, /\$ gh issue comment 7 --repo projectbluefin\/review --body ship it/);
	// Every body line is indented under "Body:", so a crafted body cannot
	// impersonate a plan line in the confirmation prompt.
	const bodyIndex = rendered.indexOf("Body:");
	for (const line of rendered.slice(bodyIndex + "Body:\n".length).split("\n")) {
		assert.match(line, /^ {4}/);
	}
});

test("renderCommentActionPlan singularises a one-target plan and omits an absent head", () => {
	const rendered = renderCommentActionPlan(createCommentActionPlan([ISSUE], "hi"));
	assert.match(rendered, /^Comment Action Plan \(1 target\):/);
	assert.equal(rendered.includes("head:"), false);
});

// ── validateCommentActionPlan: fail-closed revalidation ──────────────────────

test("validateCommentActionPlan accepts a plan whose live state is unchanged", () => {
	const plan = createCommentActionPlan([PR, ISSUE], "hello");
	const result = validateCommentActionPlan(plan, [PR, ISSUE]);
	assert.equal(result.valid, true);
	assert.deepEqual(result.errors, []);
	assert.equal(Object.isFrozen(result), true);
	assert.equal(Object.isFrozen(result.errors), true);
});

test("validateCommentActionPlan rejects a plan whose target is gone from live state", () => {
	const plan = createCommentActionPlan([PR], "hello");
	const result = validateCommentActionPlan(plan, []);
	assert.equal(result.valid, false);
	assert.deepEqual(result.errors, ["Target missing from live targets: projectbluefin/review#42"]);
});

test("validateCommentActionPlan rejects a head that moved since confirmation", () => {
	const plan = createCommentActionPlan([PR], "hello");
	const result = validateCommentActionPlan(plan, [
		{ ...PR, headSha: "0000000000000000000000000000000000000000" },
	]);
	assert.equal(result.valid, false);
	assert.equal(result.errors.length, 1);
	assert.match(result.errors[0], /PR head changed for projectbluefin\/review#42/);
	assert.match(result.errors[0], new RegExp(`plan snapshot was ${PR.headSha}`));
});

test("validateCommentActionPlan fails closed when live state has no head at all", () => {
	const plan = createCommentActionPlan([PR], "hello");
	const { headSha: _dropped, ...liveHeadless } = PR;
	const result = validateCommentActionPlan(plan, [liveHeadless as CommentTargetSnapshot]);
	assert.equal(result.valid, false);
	assert.match(result.errors[0], /No live head for projectbluefin\/review#42/);
});

test("validateCommentActionPlan rejects a target whose type changed under it", () => {
	const plan = createCommentActionPlan([ISSUE], "hello");
	const result = validateCommentActionPlan(plan, [
		{ ...ISSUE, type: "pull_request", headSha: PR.headSha },
	]);
	assert.equal(result.valid, false);
	assert.equal(result.errors.length, 1);
	assert.match(result.errors[0], /Type mismatch for projectbluefin\/review#7/);
});

test("validateCommentActionPlan reports a type flip to pull_request with no plan head", () => {
	// A plan built as an issue carries no head; if live says pull request, the
	// mismatch and the un-revalidatable head are two distinct facts.
	const plan = createCommentActionPlan([ISSUE], "hello");
	const mutated = {
		...plan,
		targets: [{ ...ISSUE, type: "pull_request" as const }],
	};
	const result = validateCommentActionPlan(mutated, [{ ...ISSUE, type: "pull_request", headSha: "aaa" }]);
	assert.equal(result.valid, false);
	assert.match(result.errors.join("\n"), /No plan head for projectbluefin\/review#7/);
});

test("validateCommentActionPlan accepts an issue target regardless of live head fields", () => {
	const plan = createCommentActionPlan([ISSUE], "hello");
	assert.equal(validateCommentActionPlan(plan, [{ ...ISSUE, headSha: "unrelated" }]).valid, true);
});

test("validateCommentActionPlan reports every offending target, not just the first", () => {
	const second: CommentTargetSnapshot = { ...PR, repo: "projectbluefin/common", number: 9 };
	const plan = createCommentActionPlan([PR, second], "hello");
	const result = validateCommentActionPlan(plan, [
		{ ...PR, headSha: "1111111111111111111111111111111111111111" },
	]);
	assert.equal(result.valid, false);
	assert.equal(result.errors.length, 2);
	assert.match(result.errors[0], /PR head changed for projectbluefin\/review#42/);
	assert.match(result.errors[1], /Target missing from live targets: projectbluefin\/common#9/);
});

test("validateCommentActionPlan ignores live targets the plan never claimed", () => {
	const plan = createCommentActionPlan([ISSUE], "hello");
	const result = validateCommentActionPlan(plan, [ISSUE, PR, { ...ISSUE, repo: "other/repo" }]);
	assert.equal(result.valid, true);
});

test("validateCommentActionPlan matches live targets by repo#number, last entry winning", () => {
	// Duplicated live keys collapse into one map entry; the last one is what a
	// plan is revalidated against. Pinned so a change to that rule is visible.
	const plan = createCommentActionPlan([PR], "hello");
	const stale = { ...PR, headSha: "2222222222222222222222222222222222222222" };
	assert.equal(validateCommentActionPlan(plan, [stale, PR]).valid, true);
	assert.equal(validateCommentActionPlan(plan, [PR, stale]).valid, false);
});
