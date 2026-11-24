const {spawn, spawnSync} = require("node:child_process");
const {readFileSync, realpathSync} = require("node:fs");
const {dirname, join} = require("node:path");
const {tmpdir} = require("node:os");
const {createInterface} = require("node:readline");
const {runInNewContext} = require("node:vm");

function startPages(codex) {
  const child = spawn(codex, [
    "--enable", "code_mode", "-c", 'plugins."pages@openai-curated-remote".enabled=true',
    "app-server", "--listen", "stdio://",
  ], {cwd: tmpdir(), stdio: ["pipe", "pipe", "ignore"]});
  const lines = createInterface({input: child.stdout});
  let requestId = 0;
  let pending;
  const fail = error => {
    if (!pending) return;
    clearTimeout(pending.timer);
    pending.reject(error);
    pending = undefined;
  };
  child.on("error", () => fail(Error("Could not start the Codex Pages runtime")));
  child.stdin.on("error", () => fail(Error("Could not reach the Codex Pages runtime")));
  child.on("close", () => fail(Error("The Codex Pages runtime stopped")));
  lines.on("line", line => {
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      return;
    }
    if (!pending || message.id !== pending.id) return;
    if (message.error) {
      fail(Error("Codex Pages runtime request failed"));
      return;
    }
    clearTimeout(pending.timer);
    pending.resolve(message.result);
    pending = undefined;
  });

  const send = message => child.stdin.write(JSON.stringify(message) + "\n");
  const deadline = Date.now() + 44_000;
  return {
    request(method, params) {
      return new Promise((resolve, reject) => {
        const id = ++requestId;
        const timer = setTimeout(() => fail(Error("Codex Pages runtime timed out")), Math.max(1, deadline - Date.now()));
        pending = {id, timer, resolve, reject};
        send({id, method, params});
      });
    },
    ready() { send({method: "initialized"}); },
    close() { lines.close(); child.stdin.destroy(); child.kill(); },
  };
}

async function main(action, url, configPath, state, profile) {
  const config = JSON.parse(readFileSync(configPath, "utf8"));
  if (!["read", "refresh"].includes(action) || !/^page_[A-Za-z0-9_-]+$/.test(config.pageId) ||
      url !== "https://chatgpt.com/space/" + config.pageId ||
      realpathSync(config.state) !== state || realpathSync(config.profileConfig) !== profile) {
    throw Error("Pages publisher does not match this orchd profile and Page");
  }
  const app = startPages(config.codex || "codex");
  try {
    await app.request("initialize", {
      clientInfo: {name: "orchd-dashboard", version: "0.1"},
      capabilities: {experimentalApi: true},
    });
    app.ready();
    const {thread} = await app.request("thread/start", {cwd: tmpdir(), ephemeral: true, sandbox: "read-only"});
    const tool = (name, args) => {
      if (args.page_id !== config.pageId) throw Error("Unexpected Pages target");
      return app.request("mcpServer/tool/call", {
        threadId: thread.id, server: "codex_apps", tool: "chatgpt_space." + name, arguments: args,
      });
    };
    if (action === "read") {
      const result = await tool("read_page", {page_id: config.pageId, view: "full"});
      const page = result.structuredContent;
      const dashboard = page?.content?.blocks?.filter(block =>
        block.metadata?.visualization_state?.file_id === config.fileReference
      ).map(block => block.metadata.visualization_state.state?.modelContent?.orchDash);
      if (result.isError || page?.metadata?.url !== url || dashboard?.length !== 1 ||
          dashboard[0]?.schema !== 1 || !Array.isArray(dashboard[0].projects)) {
        throw Error("The configured Dashboard Page could not be read");
      }
      return {title: page.metadata.title, dashboard: dashboard[0]};
    }

    const publisherPath = join(dirname(configPath), "orch-dash-pages-sync.js");
    const publish = runInNewContext(readFileSync(publisherPath, "utf8") + "\n;syncOrchDash");
    return await publish({
      mcp__codex_apps__chatgpt_space_read_page: args => tool("read_page", args),
      async mcp__codex_apps__chatgpt_space_edit_page(args) {
        const result = await tool("get_page_sharing", {page_id: config.pageId});
        const sharing = result.structuredContent;
        const emptySources = grant => grant?.complete === true && grant.sources?.length === 0;
        const privatePage = sharing?.scope === "page_and_backing_library_folder" &&
          sharing.shares?.length === 0 && sharing.next_cursor === null &&
          emptySources(sharing.inherited_access) && emptySources(sharing.inherited_workspace_link_access);
        if (result.isError || !privatePage) throw Error("Could not confirm owner-only access to the Dashboard Page");
        return tool("edit_page", args);
      },
      async exec_command({cmd}) {
        // The installed publisher quotes each argument for the shell and runs through RTK in Desktop.
        // This adapter can run the identical command without an RTK dependency.
        const result = spawnSync(cmd.replace(/^rtk proxy /, ""), {
          shell: true, encoding: "utf8", timeout: 8_000, maxBuffer: 1024 * 1024,
        });
        return {exit_code: result.status, output: result.stdout || ""};
      },
    }, {...config, waitSeconds: 0});
  } finally {
    app.close();
  }
}

main(...process.argv.slice(2)).then(
  result => console.log(JSON.stringify(result)),
  error => {
    console.log(JSON.stringify({error: error.message}));
    process.exitCode = 1;
  },
);
