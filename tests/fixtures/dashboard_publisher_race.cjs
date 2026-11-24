const assert = require("node:assert/strict");
const sync = require("../../orchd/dashboard_publisher.cjs");

const data = id => ({schema: 1, projects: [{id, cards: []}]});
const config = {pageId: "page_fixture", fileReference: "fixture"};
const url = "https://chatgpt.com/space/" + config.pageId;
let current = data("initial");
let sequence = 0;

function clients(snapshot) {
  return {
    async localCommand(argv) {
      if (argv.includes("--orchd")) {
        return {exit_code: 0, output: JSON.stringify({dashboard: await snapshot()})};
      }
      return {exit_code: 0, output: ""};
    },
    async mcp__codex_apps__chatgpt_space_read_page() {
      return {structuredContent: {
        metadata: {url},
        content: {
          page_id: config.pageId, through_sequence: sequence,
          blocks: [{id: "embed", hash: String(sequence), metadata: {
            visualization_state: {file_id: config.fileReference, state: {modelContent: {orchDash: current}}},
          }}],
        },
      }};
    },
    async mcp__codex_apps__chatgpt_space_edit_page(args) {
      const [operation] = args.operations;
      assert.equal(args.base_sequence, sequence);
      assert.equal(operation.expected_hash, String(sequence));
      current = operation.metadata.visualization_state.state.modelContent.orchDash;
      sequence++;
      return {structuredContent: {new_sequence: sequence}};
    },
  };
}

(async () => {
  let releaseSnapshot;
  let snapshotStarted;
  const started = new Promise(resolve => { snapshotStarted = resolve; });
  const older = sync(clients(() => new Promise(resolve => {
    releaseSnapshot = () => resolve(data("old"));
    snapshotStarted();
  })), config);
  await started;
  const newer = await sync(clients(async () => data("new")), config);
  assert.equal(newer.status, "updated");
  const rejected = assert.rejects(older, /changed after the first Page read/);
  releaseSnapshot();
  await rejected;
  assert.deepEqual(current, data("new"));
  assert.equal(sequence, 1);
  const unchanged = await sync(clients(async () => data("new")), config);
  assert.equal(unchanged.status, "unchanged");
  assert.equal(sequence, 1);
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
