async function syncOrchDash(tools, config, onWatching) {
  const quote = value => "'" + String(value).replaceAll("'", "'\"'\"'") + "'";
  const canonical = value => {
    if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
    if (value && typeof value === "object") {
      return "{" + Object.keys(value).sort().map(key => JSON.stringify(key) + ":" + canonical(value[key])).join(",") + "}";
    }
    return JSON.stringify(value);
  };

  function localCommand(argv, yieldTime) {
    return tools.localCommand ? tools.localCommand(argv) :
      tools.exec_command({cmd: "rtk proxy " + argv.map(quote).join(" "), yield_time_ms: yieldTime});
  }

  async function readPage() {
    const result = await tools.mcp__codex_apps__chatgpt_space_read_page({page_id: config.pageId, view: "full"});
    const page = result.structuredContent;
    const blocks = page?.content?.blocks?.filter(block =>
      block.metadata?.visualization_state?.file_id === config.fileReference &&
      block.metadata.visualization_state.state?.modelContent?.orchDash?.schema === 1
    );
    if (result.isError || page?.metadata?.url !== "https://chatgpt.com/space/" + config.pageId || blocks?.length !== 1) throw Error("Configured ORCH Dash embed is unavailable");
    return {page, block: blocks[0], envelope: blocks[0].metadata.visualization_state};
  }

  async function localSnapshot(args, started) {
    const argv = [config.python, config.watcher, "--state", config.state, "--orchd", config.orchd, ...args];
    let result = await localCommand(argv, 1000);
    let output = result.output;
    while (result.session_id) {
      if (started && output.includes('"watching"')) {
        await started();
        started = undefined;
      }
      result = await tools.write_stdin({session_id: result.session_id, chars: "", yield_time_ms: 5000});
      output += result.output;
    }
    if (result.exit_code === 2) return null;
    if (result.exit_code !== 0) throw Error("Dashboard snapshot command failed");
    const lines = output.trim().split("\n").map(line => JSON.parse(line));
    const snapshot = lines.at(-1);
    if (!snapshot.dashboard || snapshot.dashboard.schema !== 1) throw Error("Invalid dashboard snapshot");
    return snapshot;
  }

  async function record(action) {
    const argv = [config.python, config.receiptWriter, "--state", config.state, "--config", config.profileConfig,
                  "--page", config.pageId, action];
    const result = await localCommand(argv, 5000);
    if (result.exit_code !== 0) throw Error("Dashboard receipt " + action + " failed: " + result.output.trim());
  }

  async function publish() {
    const initial = await readPage();
    const previous = canonical(initial.envelope.state.modelContent.orchDash);
    let snapshot = await localSnapshot([]);
    if (config.waitSeconds && canonical(snapshot.dashboard) === previous) {
      snapshot = await localSnapshot(["--wait", config.waitSeconds, "--after", snapshot.digest], onWatching);
      if (!snapshot) return {status: "timed_out"};
    }

    const {page, block, envelope} = await readPage();
    const actual = canonical(envelope.state.modelContent.orchDash);
    const updated = canonical(snapshot.dashboard);
    if (actual === updated) return {status: "unchanged"};
    if (page.content.through_sequence !== initial.page.content.through_sequence || actual !== previous) {
      throw Error("ORCH Dash changed after the first Page read; run a new sync");
    }
    const newEnvelope = {...envelope, state: {
      ...envelope.state, modelContent: {...envelope.state.modelContent, orchDash: snapshot.dashboard}
    }};
    const stateBytes = encodeURIComponent(JSON.stringify(newEnvelope.state)).replace(/%[0-9A-F]{2}/g, "x").length;
    if (stateBytes > 16 * 1024) throw Error("Combined Page state exceeds 16 KiB");
    const result = await tools.mcp__codex_apps__chatgpt_space_edit_page({
      page_id: page.content.page_id, stream_kind: page.metadata.stream_kind, base_sequence: page.content.through_sequence,
      operations: [{op: "set_block_metadata", block_id: block.id, expected_hash: block.hash, metadata: {
        visualization_state: newEnvelope
      }}]
    });
    const saved = await readPage();
    const stored = canonical(saved.envelope.state.modelContent.orchDash);
    if (saved.block.id !== block.id || stored !== updated) {
      throw Error("Page update was not confirmed by readback");
    }
    const receipt = result.structuredContent;
    return {
      status: "updated", sequence: receipt?.new_sequence ?? receipt?.through_sequence,
      digest: snapshot.digest, projects: snapshot.dashboard.projects.length,
      firstQuestion: snapshot.dashboard.projects[0]?.cards?.[0]?.id ?? null
    };
  }

  await record("check");
  let outcome;
  try {
    outcome = await publish();
  } catch (error) {
    try {
      await record("failed");
    } catch (receiptError) {
      throw Error(error.message + "; " + receiptError.message);
    }
    throw error;
  }
  if (outcome.status !== "timed_out") {
    await record(outcome.status === "updated" ? "synced" : "verified");
  }
  return outcome;
}

if (typeof module !== "undefined") module.exports = syncOrchDash;
