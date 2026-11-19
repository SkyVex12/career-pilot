/** Only UI conventions; native permissions and platform checks stay in Rust. */
export const isMac = (): boolean => typeof navigator !== "undefined" && /Mac/.test(navigator.platform);
