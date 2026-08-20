export const TmuxNotifyPlugin = async ({ client, $, directory, worktree }) => {
  const HELPER_SCRIPT = `${worktree}/.opencode/plugins/opencode-notify-helper.sh`;
  const COOLDOWN_DIR = `${process.env.XDG_RUNTIME_DIR || "/tmp"}/tap-to-tmux-cooldown`;

  const getProject = () => directory.split("/").pop() || "unknown";

  const sendNotification = async (notificationType, message = "") => {
    const project = getProject();
    try {
      await $`bash "${HELPER_SCRIPT}" "${notificationType}" "${message}" "${directory}"`;
    } catch (err) {
      await client.app.log({
        body: {
          service: "tmux-notify-plugin",
          level: "error",
          message: `Notification failed: ${String(err)}`,
        },
      });
    }
  };

  const clearCooldown = async () => {
    const project = getProject();
    await $`rm -f "${COOLDOWN_DIR}/${project}" 2>/dev/null || true`;
  };

  return {
    event: async ({ event }) => {
      if (event.type !== 'file.watcher.updated' && event.type !== '') {

      }

      if (event.type === "session.idle") {
        // client.app.log({
        //   body: {
        //     service: "tmux-notify-plugin",
        //     level: "info", // Change from 'debug' to 'info' if you aren't seeing it
        //     message: JSON.stringify(event),
        //   },
        // })
        await sendNotification("idle_prompt", "OpenCode is waiting for input");
      }

      if (event.type === "session.status") {
        client.app.log('STATUS', event.status)
        if (event.status === "done" || event.status === "stopped") {
          await sendNotification("stop", "OpenCode session finished");
        }
      }

      if (event.type === "session.updated") {
        client.app.log('SESSION UPDATED', event)
        await clearCooldown();
      }
    }
  };
};

// };