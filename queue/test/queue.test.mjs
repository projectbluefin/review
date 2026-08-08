import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { generateSnapshots } from '../generate.mjs';
import { fetchOpenPullRequests } from '../lib/github.mjs';
import { buildQueue } from '../lib/queue.mjs';

function pullRequest(overrides = {}) {
  return {
    repository: 'projectbluefin/bluefin',
    number: 1,
    title: 'fix: improve queue handling',
    url: 'https://github.com/projectbluefin/bluefin/pull/1',
    updatedAt: '2026-08-03T16:00:00Z',
    labels: ['quality'],
    author: 'contributor',
    reviewState: 'review_required',
    mergeableState: 'clean',
    checkState: 'success',
    ...overrides,
  };
}

test('classifies pull requests and ranks a repository batch deterministically', () => {
  const result = buildQueue({
    generatedAt: '2026-08-03T16:30:00Z',
    pullRequests: [
      pullRequest({ number: 1, checkState: 'failure' }),
      pullRequest({ number: 2, mergeableState: 'dirty' }),
      pullRequest({ number: 3 }),
      pullRequest({ number: 4, mergeableState: 'unknown' }),
      pullRequest({ number: 5, reviewState: 'approved' }),
      pullRequest({
        repository: 'projectbluefin/dakota',
        number: 6,
        title: 'docs: update queue notes',
        url: 'https://github.com/projectbluefin/dakota/pull/6',
      }),
    ],
  });

  const itemsByNumber = new Map(result.items.map((item) => [item.number, item]));

  assert.equal(itemsByNumber.get(1).recommended_action, 'fix-ci');
  assert.equal(itemsByNumber.get(2).recommended_action, 'resolve-conflicts');
  assert.equal(itemsByNumber.get(3).recommended_action, 'review');
  assert.equal(itemsByNumber.get(4).recommended_action, 'investigate');
  assert.equal(itemsByNumber.get(5).recommended_action, 'ready-for-human-merge');
  assert.deepEqual(
    result.items.map((item) => item.id),
    [
      'projectbluefin/bluefin#1',
      'projectbluefin/bluefin#2',
      'projectbluefin/bluefin#3',
      'projectbluefin/bluefin#4',
      'projectbluefin/bluefin#5',
      'projectbluefin/dakota#6',
    ],
  );
  assert.match(itemsByNumber.get(1).ranking_reasons[0], /5 open pull requests/);
});

test('renders matching deterministic Markdown and JSON artifacts', () => {
  const input = {
    generatedAt: '2026-08-03T16:30:00Z',
    pullRequests: [pullRequest()],
  };
  const first = buildQueue(input);
  const second = buildQueue(input);
  const document = JSON.parse(first.json);

  assert.equal(document.generated_at, input.generatedAt);
  assert.deepEqual(Object.keys(document.items[0]), [
    'id',
    'repository',
    'number',
    'url',
    'title',
    'author',
    'updated_at',
    'labels',
    'review_state',
    'mergeable_state',
    'check_state',
    'recommended_action',
    'ranking_reasons',
  ]);
  assert.match(first.markdown, /Generated: 2026-08-03T16:30:00Z/);
  assert.match(first.markdown, /## projectbluefin\/bluefin/);
  assert.match(first.markdown, /https:\/\/github.com\/projectbluefin\/bluefin\/pull\/1/);
  assert.equal(first.markdown, second.markdown);
  assert.equal(first.json, second.json);
});

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function githubPullRequest(number, sha = `sha-${number}`) {
  return {
    number,
    title: `fix: pull request ${number}`,
    html_url: `https://github.com/projectbluefin/bluefin/pull/${number}`,
    updated_at: '2026-08-03T16:00:00Z',
    labels: [{ name: 'quality' }],
    user: { login: 'contributor' },
    head: { sha },
  };
}

test('fetches every pull-request page and derives GitHub evidence', async () => {
  const fetch = async (input) => {
    const url = new URL(input);
    if (url.pathname === '/repos/projectbluefin/bluefin/pulls') {
      return jsonResponse(
        url.searchParams.get('page') === '1'
          ? Array.from({ length: 100 }, (_, index) => githubPullRequest(index + 1))
          : [githubPullRequest(101)],
      );
    }
    if (url.pathname.endsWith('/reviews')) {
      return jsonResponse([]);
    }
    if (url.pathname.includes('/check-runs')) {
      return jsonResponse({ total_count: 0, check_runs: [] });
    }
    if (url.pathname.includes('/pulls/')) {
      return jsonResponse({ mergeable_state: 'clean' });
    }
    throw new Error(`Unexpected request: ${url}`);
  };

  const pullRequests = await fetchOpenPullRequests({
    fetch,
    owner: 'projectbluefin',
    repositories: ['bluefin'],
  });

  assert.equal(pullRequests.length, 101);
  assert.equal(pullRequests[0].repository, 'projectbluefin/bluefin');
  assert.equal(pullRequests[0].number, 1);
  assert.equal(pullRequests[0].author, 'contributor');
  assert.equal(pullRequests[0].reviewState, 'review_required');
  assert.equal(pullRequests[0].mergeableState, 'clean');
  assert.equal(pullRequests[0].checkState, 'success');
  assert.equal(pullRequests[100].number, 101);
});

test('uses complete current review and check-run evidence', async () => {
  const reviews = Array.from({ length: 100 }, (_, index) => ({
    id: index + 1,
    state: index === 0 ? 'APPROVED' : 'COMMENTED',
    submitted_at: '2026-08-03T16:00:00Z',
    user: { id: index === 0 ? 1 : index + 1 },
  }));
  const checkRuns = Array.from({ length: 100 }, () => ({
    status: 'completed',
    conclusion: 'success',
  }));
  const fetch = async (input) => {
    const url = new URL(input);
    if (url.pathname === '/repos/projectbluefin/bluefin/pulls') {
      return jsonResponse([githubPullRequest(1)]);
    }
    if (url.pathname.endsWith('/reviews')) {
      return jsonResponse(
        url.searchParams.get('page') === '1'
          ? reviews
          : url.searchParams.get('page') === '2'
            ? [
              {
                id: 101,
                state: 'CHANGES_REQUESTED',
                submitted_at: '2026-08-03T17:00:00Z',
                user: { id: 1 },
              },
            ]
            : [],
      );
    }
    if (url.pathname.includes('/check-runs')) {
      return jsonResponse(
        url.searchParams.get('page') === '1'
          ? { total_count: 101, check_runs: checkRuns }
          : url.searchParams.get('page') === '2'
            ? {
              total_count: 101,
              check_runs: [{ status: 'completed', conclusion: 'failure' }],
            }
            : { total_count: 0, check_runs: [] },
      );
    }
    if (url.pathname.includes('/pulls/')) {
      return jsonResponse({ mergeable_state: 'clean' });
    }
    throw new Error(`Unexpected request: ${url}`);
  };

  const [pullRequest] = await fetchOpenPullRequests({
    fetch,
    owner: 'projectbluefin',
    repositories: ['bluefin'],
  });

  assert.equal(pullRequest.reviewState, 'review_required');
  assert.equal(pullRequest.checkState, 'failure');
});

test('does not let a comment clear a current change request', async () => {
  const fetch = async (input) => {
    const url = new URL(input);
    if (url.pathname === '/repos/projectbluefin/bluefin/pulls') {
      return jsonResponse([githubPullRequest(1)]);
    }
    if (url.pathname.endsWith('/reviews')) {
      return jsonResponse([
        {
          id: 1,
          state: 'CHANGES_REQUESTED',
          submitted_at: '2026-08-03T16:00:00Z',
          user: { id: 1 },
        },
        {
          id: 2,
          state: 'COMMENTED',
          submitted_at: '2026-08-03T17:00:00Z',
          user: { id: 1 },
        },
        {
          id: 3,
          state: 'APPROVED',
          submitted_at: '2026-08-03T18:00:00Z',
          user: { id: 2 },
        },
      ]);
    }
    if (url.pathname.includes('/check-runs')) {
      return jsonResponse({ total_count: 0, check_runs: [] });
    }
    if (url.pathname.includes('/pulls/')) {
      return jsonResponse({ mergeable_state: 'clean' });
    }
    throw new Error(`Unexpected request: ${url}`);
  };

  const [pullRequest] = await fetchOpenPullRequests({
    fetch,
    owner: 'projectbluefin',
    repositories: ['bluefin'],
  });

  assert.equal(pullRequest.reviewState, 'review_required');
});

test('rejects failed list responses and preserves existing snapshots', async () => {
  const failingFetch = async () => jsonResponse({ message: 'unavailable' }, 503);
  await assert.rejects(
    () =>
      fetchOpenPullRequests({
        fetch: failingFetch,
        owner: 'projectbluefin',
        repositories: ['bluefin'],
      }),
    /GitHub request failed for projectbluefin\/bluefin pull requests/,
  );

  const outputDirectory = await mkdtemp(path.join(tmpdir(), 'agent-pr-queue-'));
  const markdownPath = path.join(outputDirectory, 'queue.md');
  const jsonPath = path.join(outputDirectory, 'queue.json');
  await writeFile(markdownPath, 'previous markdown\n');
  await writeFile(jsonPath, 'previous json\n');

  await assert.rejects(
    () =>
      generateSnapshots({
        fetch: failingFetch,
        owner: 'projectbluefin',
        repositories: ['bluefin'],
        outputDirectory,
      }),
    /GitHub request failed/,
  );

  assert.equal(await readFile(markdownPath, 'utf8'), 'previous markdown\n');
  assert.equal(await readFile(jsonPath, 'utf8'), 'previous json\n');
  await rm(outputDirectory, { recursive: true, force: true });
});

test('preserves an unchanged snapshot instead of refreshing generated_at', async () => {
  const outputDirectory = await mkdtemp(path.join(tmpdir(), 'agent-pr-queue-'));
  const fetch = async (input) => {
    const url = new URL(input);
    if (url.pathname === '/repos/projectbluefin/bluefin/pulls') {
      return jsonResponse([githubPullRequest(1)]);
    }
    if (url.pathname.endsWith('/reviews')) {
      return jsonResponse([]);
    }
    if (url.pathname.includes('/check-runs')) {
      return jsonResponse({ total_count: 0, check_runs: [] });
    }
    if (url.pathname.includes('/pulls/')) {
      return jsonResponse({ mergeable_state: 'clean' });
    }
    throw new Error(`Unexpected request: ${url}`);
  };

  await generateSnapshots({
    fetch,
    owner: 'projectbluefin',
    repositories: ['bluefin'],
    outputDirectory,
    generatedAt: '2026-08-03T16:00:00Z',
  });
  const originalMarkdown = await readFile(path.join(outputDirectory, 'queue.md'), 'utf8');
  const originalJson = await readFile(path.join(outputDirectory, 'queue.json'), 'utf8');

  const result = await generateSnapshots({
    fetch,
    owner: 'projectbluefin',
    repositories: ['bluefin'],
    outputDirectory,
    generatedAt: '2026-08-03T16:15:00Z',
  });

  assert.equal(result.changed, false);
  assert.equal(await readFile(path.join(outputDirectory, 'queue.md'), 'utf8'), originalMarkdown);
  assert.equal(await readFile(path.join(outputDirectory, 'queue.json'), 'utf8'), originalJson);
  await rm(outputDirectory, { recursive: true, force: true });
});

test('refresh workflow is a constrained static snapshot publisher', async () => {
  const workflow = await readFile(
    new URL('../../.github/workflows/update-pr-queue.yml', import.meta.url),
    'utf8',
  );

  assert.match(workflow, /cron: '\*\/15 \* \* \* \*'/);
  assert.match(workflow, /contents: read/);
  assert.match(workflow, /pages: write/);
  assert.match(workflow, /id-token: write/);
  assert.match(workflow, /node --test queue\/test\/queue\.test\.mjs/);
  assert.match(workflow, /QUEUE_OWNER: projectbluefin/);
  assert.match(workflow, /ref: main/);
  assert.match(workflow, /actions\/configure-pages@45bfe0192ca1faeb007ade9deae92b16b8254a0d/);
  assert.match(workflow, /actions\/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9/);
  assert.match(workflow, /actions\/deploy-pages@cd2ce8fcbc39b97be8ca5fce6e763baed58fa128/);
  assert.doesNotMatch(workflow, /\bgit push\b/);
  assert.match(workflow, /repository_dispatch:\s+types: \[renovate-completed\]/);
  assert.doesNotMatch(workflow, /\bpull_request:/);
  assert.doesNotMatch(workflow, /pull_request_target/);
});

test('static host root redirects to the Markdown queue', async () => {
  const index = await readFile(
    new URL('../../public/index.html', import.meta.url),
    'utf8',
  );

  assert.match(index, /url=queue\.md/);
  assert.match(index, /href="queue\.md"/);
});
