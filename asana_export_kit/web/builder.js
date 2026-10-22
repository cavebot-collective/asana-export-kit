/* Offline command construction; no tokens, network requests or command execution. */
(function (root) {
  const categories = ['stories', 'attachments', 'portfolios', 'goals', 'statuses', 'briefs'];
  function build(c) {
    if (!c.output || /[\r\n\0]/.test(c.output)) throw Error('Choose an output folder without line breaks.');
    const rpm = Number(c.rpm);
    if (!Number.isFinite(rpm) || rpm <= 0) throw Error('Request budget must be positive.');
    const args = ['--rpm', String(rpm)];
    const workspaces = (c.workspaces || '').trim().split(/[\s,]+/).filter(Boolean);
    for (const id of workspaces) {
      if (!/^\d+$/.test(id)) throw Error('Workspace IDs must be numeric.');
      args.push('--workspace', id);
    }
    for (const category of categories) if (!c[category]) args.push('--omit', category);
    if (!c.timeEntries) args.push('--skip-time-entries');
    if (c.extended) args.push('--extended');
    if (c.attachments && c.downloads) args.push('--download-attachments');
    const cap = Number(c.maxBytes || 0);
    if (!Number.isSafeInteger(cap) || cap < 0) throw Error('File limit must be a nonnegative safe integer.');
    args.push('--max-attachment-bytes', String(cap));
    if (c.acceptPartial) args.push('--allow-partial');
    const prefix = ['python', '-m', 'asana_export_kit'];
    if (c.schedule) {
      const interval = Number(c.interval), runs = Number(c.runs || 0);
      if (!Number.isFinite(interval) || interval <= 0 || !Number.isSafeInteger(runs) || runs < 0) throw Error('Use a positive interval and nonnegative run count.');
      if (c.auth === 'oauth') throw Error('Interval runs currently use a PAT: choose hidden prompt or environment token.');
      prefix.push('watch', '--root', c.output, '--interval', String(interval), '--runs', String(runs));
      if (c.auth === 'prompt') prefix.push('--prompt-token');
      if (c.remote) prefix.push('--remote', c.remote);
      return [...prefix, '--', ...args];
    }
    prefix.push('export', '--out', c.output);
    if (c.auth === 'oauth') prefix.push('--oauth');
    if (c.auth === 'prompt') prefix.push('--prompt-token');
    if (c.remote) prefix.push('--remote', c.remote);
    return [...prefix, ...args];
  }
  function quote(argument, shell) {
    if (/[\r\n\0]/.test(argument)) throw Error('Arguments cannot contain line breaks or NUL.');
    return shell === 'powershell' ? "'" + argument.replace(/'/g, "''") + "'" : "'" + argument.replace(/'/g, "'\"'\"'") + "'";
  }
  function command(args, shell = 'powershell') {
    return (shell === 'powershell' ? '& ' : '') + args.map(x => quote(x, shell)).join(' ');
  }
  const api = {build, command, categories};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.AsanaBuilder = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
