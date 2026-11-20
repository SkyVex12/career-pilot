import rubyIcon from "../assets/xruby-icon.svg";

/** Approved CoRuby coral gem; the product name is rendered separately. */
export function RubyMark() {
  const size = 30;
  return <img src={rubyIcon} width={size} height={size} alt="" aria-hidden="true" draggable={false} style={{ pointerEvents: "none" }} />;
}

export function CoRubyWordmark() {
  return <span>Co<span style={{ color: "var(--wordmark)" }}>Ruby</span></span>;
}
