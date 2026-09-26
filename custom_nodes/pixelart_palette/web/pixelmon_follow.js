// pixelmon: follow running jobs.
// pixelmon submits jobs straight to ComfyUI's API, so the canvas never shows them. When a job made by
// pixelmon starts (its Save Image prefix begins with "pixelmon/"), load that job's exact graph — every
// real value: LoRA, strengths, steps, CFG, ControlNet, mask, palette node — onto the canvas.
// Toggle: Settings -> pixelmon -> "Show each running pixelmon job on the canvas".
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const SETTING = "pixelmon.followJobs";

function isPixelmon(prompt) {
  return Object.values(prompt || {}).some(n =>
    n && n.class_type === "SaveImage" && String(n.inputs?.filename_prefix || "").startsWith("pixelmon/"));
}

async function promptFor(id) {
  // a running job is in /queue; if it already finished, it's in /history
  try {
    const q = await (await api.fetchApi("/queue")).json();
    for (const item of [...(q.queue_running || []), ...(q.queue_pending || [])])
      if (item[1] === id) return item[2];
    const h = await (await api.fetchApi(`/history/${id}`)).json();
    return h?.[id]?.prompt?.[2];
  } catch { return null; }
}

app.registerExtension({
  name: "pixelmon.followJobs",
  settings: [{
    id: SETTING, category: ["pixelmon", "Canvas", "Follow jobs"], name: "Show each running pixelmon job on the canvas",
    tooltip: "Replaces the canvas with the graph of every pixelmon job as it starts. Turn off to build your own workflows.",
    type: "boolean", defaultValue: true,
  }],
  setup() {
    let last = null;
    api.addEventListener("execution_start", async ({ detail }) => {
      const id = detail?.prompt_id;
      if (!id || id === last || app.extensionManager?.setting?.get?.(SETTING) === false) return;
      const prompt = await promptFor(id);
      if (!isPixelmon(prompt)) return;
      last = id;
      try { await app.loadApiJson(prompt, `pixelmon job ${id.slice(0, 8)}`); }
      catch (e) { console.warn("pixelmon follow: couldn't load the job graph", e); }
    });
  },
});
