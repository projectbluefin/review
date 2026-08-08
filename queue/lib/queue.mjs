const ACTION_ORDER = new Map([
  ['fix-ci', 0],
  ['resolve-conflicts', 1],
  ['review', 2],
  ['investigate', 3],
  ['ready-for-human-merge', 4],
]);

const MERGEABLE_STATES = new Set(['clean', 'dirty', 'unknown']);
const REVIEW_STATES = new Set(['approved', 'review_required', 'unknown']);
const CHECK_STATES = new Set(['success', 'failure', 'unknown']);

function requiredString(value, name) {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new TypeError(`${name} must be a non-empty string`);
  }
  return value;
}

function validTimestamp(value, name) {
  const timestamp = requiredString(value, name);
  if (Number.isNaN(Date.parse(timestamp))) {
    throw new TypeError(`${name} must be an ISO 8601 timestamp`);
  }
  return timestamp;
}

function validState(value, states, name) {
  if (!states.has(value)) {
    throw new TypeError(`${name} has an unsupported value`);
  }
  return value;
}

function validLabels(value) {
  if (!Array.isArray(value) || value.some((label) => typeof label !== 'string')) {
    throw new TypeError('labels must be an array of strings');
  }
  return value;
}

function validRepository(value) {
  const repository = requiredString(value, 'repository');
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository)) {
    throw new TypeError('repository must be an owner/name identifier');
  }
  return repository;
}

function validNumber(value) {
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new TypeError('number must be a positive integer');
  }
  return value;
}

function validUrl(value) {
  const url = requiredString(value, 'url');
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'https:') {
      throw new TypeError('url must use HTTPS');
    }
  } catch {
    throw new TypeError('url must be a valid HTTPS URL');
  }
  return url;
}

function classifyAction({ checkState, mergeableState, reviewState }) {
  if (checkState === 'failure') {
    return 'fix-ci';
  }
  if (mergeableState === 'dirty') {
    return 'resolve-conflicts';
  }
  if (
    checkState === 'unknown' ||
    mergeableState === 'unknown' ||
    reviewState === 'unknown'
  ) {
    return 'investigate';
  }
  if (reviewState === 'approved') {
    return 'ready-for-human-merge';
  }
  return 'review';
}

function actionReason(action) {
  return {
    'fix-ci': 'one or more checks failed',
    'resolve-conflicts': 'GitHub reports merge conflicts',
    review: 'review is required',
    investigate: 'GitHub evidence is incomplete',
    'ready-for-human-merge': 'approved with successful checks',
  }[action];
}

function normalizePullRequest(value) {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new TypeError('pull request must be an object');
  }

  const repository = validRepository(value.repository);
  const number = validNumber(value.number);
  const title = requiredString(value.title, 'title');
  const url = validUrl(value.url);
  const updatedAt = validTimestamp(value.updatedAt, 'updatedAt');
  const labels = validLabels(value.labels);
  const author = requiredString(value.author, 'author');
  const reviewState = validState(value.reviewState, REVIEW_STATES, 'reviewState');
  const mergeableState = validState(
    value.mergeableState,
    MERGEABLE_STATES,
    'mergeableState',
  );
  const checkState = validState(value.checkState, CHECK_STATES, 'checkState');

  return {
    repository,
    number,
    title,
    url,
    updatedAt,
    labels,
    author,
    reviewState,
    mergeableState,
    checkState,
  };
}

function markdownText(value) {
  return value.replaceAll('|', '\\|').replaceAll('\n', ' ');
}

function renderMarkdown(items, generatedAt) {
  const groups = new Map();
  for (const item of items) {
    const group = groups.get(item.repository) ?? [];
    group.push(item);
    groups.set(item.repository, group);
  }

  const sections = [
    '# Project Bluefin PR Queue',
    '',
    `Generated: ${generatedAt}`,
    '',
    'GitHub remains authoritative. Verify a selected pull request in GitHub before acting.',
  ];

  for (const [repository, group] of groups) {
    sections.push(
      '',
      `## ${repository}`,
      '',
      '| PR | Action | Title | Why |',
      '| --- | --- | --- | --- |',
    );
    for (const item of group) {
      sections.push(
        `| [#${item.number}](${item.url}) | ${item.recommended_action} | ${markdownText(item.title)} | ${item.ranking_reasons.join('; ')} |`,
      );
    }
  }

  return `${sections.join('\n')}\n`;
}

export function buildQueue({ pullRequests, generatedAt }) {
  if (!Array.isArray(pullRequests)) {
    throw new TypeError('pullRequests must be an array');
  }
  validTimestamp(generatedAt, 'generatedAt');

  const normalized = pullRequests.map(normalizePullRequest);
  const repositoryCounts = new Map();
  for (const item of normalized) {
    repositoryCounts.set(item.repository, (repositoryCounts.get(item.repository) ?? 0) + 1);
  }

  const items = normalized
    .map((item) => {
      const recommendedAction = classifyAction(item);
      const count = repositoryCounts.get(item.repository);
      return {
        id: `${item.repository}#${item.number}`,
        repository: item.repository,
        number: item.number,
        url: item.url,
        title: item.title,
        author: item.author,
        updated_at: item.updatedAt,
        labels: item.labels,
        review_state: item.reviewState,
        mergeable_state: item.mergeableState,
        check_state: item.checkState,
        recommended_action: recommendedAction,
        ranking_reasons: [
          `repository has ${count} open pull request${count === 1 ? '' : 's'}`,
          actionReason(recommendedAction),
        ],
      };
    })
    .sort((left, right) => {
      const countDifference =
        repositoryCounts.get(right.repository) - repositoryCounts.get(left.repository);
      if (countDifference !== 0) {
        return countDifference;
      }
      const actionDifference =
        ACTION_ORDER.get(left.recommended_action) - ACTION_ORDER.get(right.recommended_action);
      if (actionDifference !== 0) {
        return actionDifference;
      }
      const timestampDifference = left.updated_at.localeCompare(right.updated_at);
      if (timestampDifference !== 0) {
        return timestampDifference;
      }
      return left.id.localeCompare(right.id);
    });

  return {
    items,
    markdown: renderMarkdown(items, generatedAt),
    json: `${JSON.stringify({ generated_at: generatedAt, items }, null, 2)}\n`,
  };
}
