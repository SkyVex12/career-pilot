import { useId } from "react";
import { Button, IconButton, SegmentedControl } from "./ui";
import { RESPONSE_LENGTHS, useAnswerStyle, useResponseLength, useViewPrefs } from "./viewPreferences";
import "./windowSettingControls.css";

export function TransparencyControl({ opacity, disabled, onChange, onCommit }: {
  opacity: number; disabled?: boolean; onChange: (opacity: number) => void; onCommit?: () => void;
}) {
  const id = useId();
  return <div className="window-transparency">
    <label htmlFor={id}>Transparency</label>
    <input id={id} type="range" min={10} max={100} step={5} value={Math.round(opacity * 100)} dir="ltr" disabled={disabled}
      aria-valuetext={opacity >= 1 ? "Solid" : String(Math.round((1 - opacity) * 100)) + "% transparent"} aria-describedby={id + "-hint"}
      onChange={event => onChange(Number(event.target.value) / 100)} onPointerUp={onCommit} onBlur={onCommit} />
    <div id={id + "-hint"} className="window-transparency-ends"><span>Clear</span><span>Solid</span></div>
  </div>;
}

export function ReadingSettings({ running, guest = false, busy, run }: {
  running: boolean; guest?: boolean; busy: boolean; run: (action: () => Promise<unknown>) => void;
}) {
  const view = useViewPrefs();
  const [answerStyle, changeAnswerStyle] = useAnswerStyle(running);
  const [responseLength, changeResponseLength] = useResponseLength(running);
  return <div className="window-reading-settings">
    <div className="window-text-size"><span>Text size</span><div>
      <IconButton label="Smaller text (Ctrl+minus)" icon="A−" size="sm" onClick={() => view.stepText(-1)} />
      <Button size="sm" variant="ghost" onClick={() => view.stepText(0)}>Reset</Button>
      <IconButton label="Larger text (Ctrl+plus)" icon="A+" size="sm" onClick={() => view.stepText(1)} />
    </div></div>
    {!guest && <div className="window-answer-format"><span>Answer format</span>
      <SegmentedControl equal label="Answer format" value={answerStyle} options={[["bullets", "Bullets"], ["paragraph", "Paragraphs"]] as const}
        size="sm" disabled={busy} onChange={value => run(() => changeAnswerStyle(value))} />
    </div>}
    {!guest && <div className="window-answer-format"><span>Response length</span>
      <SegmentedControl equal label="Response length" value={responseLength} options={RESPONSE_LENGTHS}
        size="sm" disabled={busy} onChange={value => run(() => changeResponseLength(value))} />
    </div>}
  </div>;
}
