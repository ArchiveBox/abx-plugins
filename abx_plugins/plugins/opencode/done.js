// Native OpenCode custom tool, installed only in a bounded task's config directory.
export default {
  description:
    "Finish the current task after completing the requested work, or after explaining why you are stuck. Leave the browser and tabs open.",
  args: {},
  async execute() {
    return "Task finished. Leave the browser and tabs open.";
  },
};
