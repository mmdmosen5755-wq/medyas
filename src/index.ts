import { Container, getContainer } from "@cloudflare/containers";

// Secrets/vars are set with `wrangler secret put NAME` (see DEPLOY_CLOUDFLARE.md) and handed to the Python app as env vars.
interface Env {
  MEDYAS: DurableObjectNamespace<MedyasContainer>;
  DATABASE_URL: string;
  SECRET_KEY: string;
  ADMIN_PASSWORD: string;
  ANTHROPIC_API_KEY?: string;
  AI_PROXY?: string;
  EITAA_PROXY?: string;
  EITAA_API_BASE?: string;
  EITAA_WEB_BASE?: string;
  BOT_TOKEN?: string;
  ADMIN_IDS?: string;
}

const PASS_THROUGH = [
  "DATABASE_URL", "SECRET_KEY", "ADMIN_PASSWORD", "ANTHROPIC_API_KEY", "AI_PROXY",
  "EITAA_PROXY", "EITAA_API_BASE", "EITAA_WEB_BASE", "BOT_TOKEN", "ADMIN_IDS",
] as const;

export class MedyasContainer extends Container<Env> {
  defaultPort = 8000;
  // The bot must keep polling Telegram, so this container must never sleep. The cron trigger below pings it
  // every minute (each request renews the activity timeout); sleepAfter is only the safety margin between pings.
  sleepAfter = "30m";

  constructor(ctx: ConstructorParameters<typeof Container<Env>>[0], env: Env) {
    super(ctx, env);
    const vars: Record<string, string> = { COOKIE_SECURE: "1" };
    for (const k of PASS_THROUGH) {
      const v = env[k];
      if (typeof v === "string" && v !== "") vars[k] = v;
    }
    this.envVars = vars;
  }

  override onStart() { console.log("medyas container started"); }
  override onStop() { console.log("medyas container stopped"); }
  override onError(error: unknown) { console.error("medyas container error", error); }
}

// ONE fixed id = exactly one container instance (long-polling must not run twice).
const instance = (env: Env) => getContainer(env.MEDYAS, "main");

export default {
  // The panel: every request is forwarded to the Python app.
  async fetch(request: Request, env: Env): Promise<Response> {
    return instance(env).fetch(request);
  },
  // Keep-alive + restart-if-dead: wakes the container if it is asleep or was recycled.
  async scheduled(_event: ScheduledController, env: Env): Promise<void> {
    try {
      await instance(env).fetch(new Request("http://container/health"));
    } catch (e) {
      console.error("keepalive failed", e);
    }
  },
} satisfies ExportedHandler<Env>;
