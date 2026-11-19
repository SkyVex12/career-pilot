import rubyIcon from "../assets/xruby-icon.svg";

/** Shared, unmodified marketing-site brand asset. */
export function RubyMark() {
  const size = 30;
  return <img src={rubyIcon} width={size} height={size} alt="" aria-hidden="true" draggable={false} style={{ pointerEvents: "none" }} />;
}
