// The faked Rust core for component tests (tests/setup.ts installs it). A test routes commands
// with `ipc.handle(cmd, fn)` and API calls with `ipc.api(method, pathPattern, fn)`; every call is
// recorded in `ipc.calls`. Unrouted commands return null; unrouted API calls fail like a 404.

type Handler = (args: Record<string, unknown>) => unknown;
type ApiHandler = (path: string, body: unknown) => unknown;

const commands = new Map<string, Handler>();
let routes: { method: string; pattern: RegExp; fn: ApiHandler }[] = [];

export const ipc = {
  calls: [] as { cmd: string; args: Record<string, unknown> }[],
  handle(cmd: string, fn: Handler) {
    commands.set(cmd, fn);
  },
  /** `pattern` matches the path without its query string, e.g. /^\/sessions$/. */
  api(method: string, pattern: RegExp, fn: ApiHandler) {
    routes.push({ method, pattern, fn });
  },
  apiCalls(method?: string): { method: string; path: string; body: unknown }[] {
    return this.calls
      .filter((c) => c.cmd === "api_call" && (!method || c.args.method === method))
      .map((c) => ({ method: String(c.args.method), path: String(c.args.path), body: c.args.body }));
  },
  dispatch(cmd: string, args: Record<string, unknown>): unknown {
    this.calls.push({ cmd, args });
    if (cmd === "api_call") {
      const method = String(args.method);
      const path = String(args.path);
      const bare = path.split("?")[0];
      const route = [...routes].reverse().find((r) => r.method === method && r.pattern.test(bare));
      if (!route) throw `No route for ${method} ${path} (HTTP 404)`;
      return route.fn(path, args.body);
    }
    const fn = commands.get(cmd);
    return fn ? fn(args) : null;
  },
  reset() {
    commands.clear();
    routes = [];
    this.calls = [];
  },
};

/** A promise that never settles: a request still on its way. */
export const pending = () => new Promise<never>(() => {});
