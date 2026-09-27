const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

function readNumber(file) {
  const value = Number(fs.readFileSync(file, "utf8").trim());
  if (!Number.isSafeInteger(value) || value < 0) throw new Error(`Invalid memory value in ${file}`);
  return value;
}

function memoryHeadroomBytes({ includeCgroup = true } = {}) {
  if (process.platform !== "linux") {
    // Node does not expose independently backed swap on other platforms.
    const available = process.availableMemory?.() ?? os.freemem();
    if (!Number.isSafeInteger(available) || available < 0) throw new Error("Invalid available memory");
    return available;
  }

  const meminfo = Object.fromEntries(
    fs.readFileSync("/proc/meminfo", "utf8").split("\n")
      .filter((line) => line.includes(":"))
      .map((line) => {
        const [name, value] = line.split(":", 2);
        return [name, Number.parseInt(value, 10) * 1024];
      }),
  );
  if (![meminfo.MemAvailable, meminfo.MemTotal, meminfo.SwapFree].every(Number.isSafeInteger))
    throw new Error("Linux memory availability is unreadable");

  // zram is backed by host RAM and must not be counted again as extra swap.
  let diskSwapFree = 0;
  for (const line of fs.readFileSync("/proc/swaps", "utf8").split("\n").slice(1)) {
    const [source, , size, used] = line.trim().split(/\s+/);
    if (!source || /^zram\d+$/.test(path.basename(source))) continue;
    const free = (Number(size) - Number(used)) * 1024;
    if (!Number.isSafeInteger(free) || free < 0) throw new Error("Linux swap availability is unreadable");
    diskSwapFree += free;
  }
  const swapFree = Math.min(meminfo.SwapFree, diskSwapFree);
  let memoryRoom = meminfo.MemAvailable;
  let swapRoom = swapFree;
  if (!includeCgroup) return memoryRoom + swapRoom;

  const root = "/sys/fs/cgroup";
  if (fs.existsSync(`${root}/cgroup.controllers`)) {
    const entry = fs.readFileSync("/proc/self/cgroup", "utf8").split("\n").find((line) => line.startsWith("0::"));
    if (!entry) throw new Error("Linux cgroup path is unreadable");
    const current = path.resolve(root, `.${entry.slice(3)}`);
    if (current !== root && !current.startsWith(`${root}/`)) throw new Error("Invalid Linux cgroup path");
    if (!fs.existsSync(current)) throw new Error("Linux cgroup directory is unreadable");
    for (let dir = current; dir.startsWith(root); dir = path.dirname(dir)) {
      const limitFile = `${dir}/memory.max`;
      if (!fs.existsSync(limitFile)) {
        if (dir === root) break;
        continue;
      }
      const limit = fs.readFileSync(limitFile, "utf8").trim();
      if (limit !== "max") {
        const memoryLimit = BigInt(limit);
        const memoryCurrent = readNumber(`${dir}/memory.current`);
        if (memoryLimit < 0)
          throw new Error("Linux cgroup memory limit is unreadable");
        const room = memoryLimit > BigInt(memoryCurrent) ? memoryLimit - BigInt(memoryCurrent) : 0n;
        memoryRoom = Number(room < BigInt(memoryRoom) ? room : BigInt(memoryRoom));
      }
      // RAM and swap can be limited by different ancestors, including an
      // unlimited-RAM cgroup with swap disabled. Apply their bounds separately.
      const swapLimit = fs.readFileSync(`${dir}/memory.swap.max`, "utf8").trim();
      if (swapLimit !== "max") {
        const limitBytes = BigInt(swapLimit);
        if (limitBytes < 0) throw new Error("Linux cgroup swap limit is unreadable");
        const currentBytes = BigInt(readNumber(`${dir}/memory.swap.current`));
        const room = limitBytes > currentBytes ? limitBytes - currentBytes : 0n;
        swapRoom = Number(room < BigInt(swapRoom) ? room : BigInt(swapRoom));
      }
      if (dir === root) break;
    }
  } else if (fs.existsSync(`${root}/memory/memory.limit_in_bytes`)) {
    // cgroup v1's RAM limit is safe without assuming extra swap capacity.
    const limit = BigInt(fs.readFileSync(`${root}/memory/memory.limit_in_bytes`, "utf8").trim());
    const current = readNumber(`${root}/memory/memory.usage_in_bytes`);
    if (limit < 0) throw new Error("Linux cgroup memory limit is unreadable");
    // Even a limit equal to host RAM may be nearly full. Keep v1's huge
    // unlimited sentinel as a BigInt until the result is capped to host headroom.
    const room = limit > BigInt(current) ? limit - BigInt(current) : 0n;
    memoryRoom = Number(room < BigInt(memoryRoom) ? room : BigInt(memoryRoom));
    swapRoom = 0;
  }
  const headroom = memoryRoom + swapRoom;
  if (!Number.isSafeInteger(headroom) || headroom < 0) throw new Error("Invalid memory headroom");
  return headroom;
}

module.exports = { memoryHeadroomBytes };
