import { invoke } from "@tauri-apps/api/core";
import { inTauri } from "./api";

export const startWindowDrag = () => inTauri ? invoke<void>("start_window_drag") : Promise.resolve();

const controls = [
  "button", "a", "input", "select", "textarea", "summary", "label",
  '[contenteditable]:not([contenteditable="false"])',
  ...["button", "link", "combobox", "listbox", "option", "menu", "menuitem", "menuitemcheckbox", "menuitemradio", "checkbox", "radio", "switch", "slider", "spinbutton", "scrollbar", "textbox", "searchbox", "tab", "treeitem"].map(role => `[role="${role}"]`),
].join(",");

/** Blank frame space uses the same native move operation as the draggable ruby. */
export function installWindowDragging() {
  const down = (event: PointerEvent) => {
    if (event.button !== 0 || !(event.target instanceof Element) || event.target.closest(controls)) return;
    const region = event.target.closest("[data-native-drag-region]");
    // Only headers claim their text descendants; body content remains selectable.
    if (!region || (event.target !== region && region.getAttribute("data-native-drag-region") !== "header")) return;
    event.preventDefault();
    void startWindowDrag().catch(error => console.error("Could not move the window:", error));
  };
  window.addEventListener("pointerdown", down);
  return () => window.removeEventListener("pointerdown", down);
}
