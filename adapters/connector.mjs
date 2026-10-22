// Generic adapter for the connected Asana tool surface.
// Host supplies call(name, args) and persists each record through save(record).
// Unlike REST, this interface cannot paginate comments or project sections.
export function unwrap(result) {
  if (result.structuredContent) return result.structuredContent;
  for (const c of result.content || []) {
    if (c.type === "text") { try { return JSON.parse(c.text); } catch {} }
  }
  throw new Error("Connector returned no JSON");
}
export async function runBatch(jobs, call, save, concurrency = 4) {
  let cursor = 0;
  async function worker() {
    while (cursor < jobs.length) {
      const job = jobs[cursor++];
      const record = { ...job, retrieved_at: new Date().toISOString() };
      try { record.result = await call(job.tool, job.args); }
      catch (error) { record.failure = String(error); }
      await save(record);
    }
  }
  await Promise.all(Array.from({length: Math.max(1, Math.min(10, concurrency))}, worker));
}
export async function collect(call, save) {
  const tasks = new Set(), projects = new Set();
  async function invoke(tool, args, key) {
    const record = {key, tool, args, retrieved_at: new Date().toISOString()};
    try { record.result = await call(tool, args); }
    catch (error) { record.failure = String(error); }
    await save(record);
    if (record.failure || record.result.isError) return null;
    return unwrap(record.result);
  }
  async function pages(tool, args, key) {
    const data = [], seen = new Set();
    let offset;
    do {
      const r = await invoke(tool, {...args, limit:100, ...(offset ? {offset} : {})}, key);
      if (!r) return data;
      data.push(...(r.data || []));
      offset = r.next_page?.offset;
      if (offset && seen.has(offset)) throw new Error("Repeated pagination offset: "+key);
      seen.add(offset);
    } while(offset);
    return data;
  }
  await invoke("mcp__codex_apps__asana_get_me", {}, "identity");
  const users = await pages("mcp__codex_apps__asana_get_users", {}, "users");
  await pages("mcp__codex_apps__asana_get_teams", {}, "teams");
  await pages("mcp__codex_apps__asana_get_portfolios", {}, "portfolios");
  await pages("mcp__codex_apps__asana_get_workspace_agents", {}, "agents");
  for (const archived of [false,true]) {
    for (const p of await pages("mcp__codex_apps__asana_get_projects",{archived},"project-index")) projects.add(p.gid);
  }
  for (const gid of projects) {
    await invoke("mcp__codex_apps__asana_get_project",{project_id:gid,include_sections:true},"projects/"+gid);
    for (const t of await pages("mcp__codex_apps__asana_get_tasks",{project:gid,completed_since:"1970-01-01T00:00:00.000Z"},"project-tasks/"+gid)) tasks.add(t.gid);
    await pages("mcp__codex_apps__asana_get_attachments",{parent:gid},"attachments/"+gid);
  }
  for (const u of users) {
    for (const t of await pages("mcp__codex_apps__asana_get_tasks",{assignee:u.gid,completed_since:"1970-01-01T00:00:00.000Z"},"assigned-tasks/"+u.gid)) tasks.add(t.gid);
  }
  // Set iteration visits newly added subtasks and graph neighbors exactly once.
  for (const gid of tasks) {
    const r = await invoke("mcp__codex_apps__asana_get_task",{task_id:gid,include_comments:true,comment_limit:50,include_subtasks:true},"tasks/"+gid);
    for (const t of [...(r?.data?.subtasks||[]),...(r?.data?.dependencies||[]),...(r?.data?.dependents||[])]) tasks.add(t.gid);
    if (r?.data?.parent) tasks.add(r.data.parent.gid);
    await pages("mcp__codex_apps__asana_get_attachments",{parent:gid},"attachments/"+gid);
  }
  return {projects:projects.size,tasks:tasks.size,status:"partial",
    limitations:["Connector-selected workspace only","Comments capped at 50; full activity unavailable",
      "Project sections capped at 50","Subtask paging is not exposed","No full status history, goals, briefs or binary download",
      "Unassigned projectless untagged tasks cannot be exhaustively discovered"]};
}

