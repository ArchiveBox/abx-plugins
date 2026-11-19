// Keep native option input on the element after hover repositions a popup.
async function clickReadyOption(page, element, remaining) {
  let point = await element.clickablePoint();
  let nextCheck = 0;
  while (true) {
    remaining();
    await page.mouse.move(point.x, point.y);
    const hit = await page.waitForFunction(
      (el, point, nextCheck) => {
        if (Date.now() < nextCheck) return false;
        if (!el.isConnected) throw new Error("Canva export option detached");
        if (el.disabled || el.getAttribute("aria-disabled") === "true") return false;
        for (let ancestor = el; ancestor; ancestor = ancestor.parentElement) {
          if (ancestor.getAnimations().some((animation) => animation.playState === "running")) return false;
        }
        const box = el.getBoundingClientRect();
        if (!box.width || !box.height || !el.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2))) return false;
        return { matches: el.contains(document.elementFromPoint(point.x, point.y)) };
      },
      { polling: 100, timeout: remaining() },
      element,
      point,
      nextCheck,
    );
    let matches;
    try {
      ({ matches } = await hit.jsonValue());
    } finally {
      await hit.dispose();
    }
    if (matches) break;
    // Repositioning is readiness polling, not a replayed click. A persistently
    // obstructed CDP point must not spin faster than the existing poll cadence.
    nextCheck = Date.now() + 100;
    point = await element.clickablePoint();
  }
  await page.mouse.click(point.x, point.y);
}

module.exports = { clickReadyOption };
