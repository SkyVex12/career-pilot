import { useCallback, useEffect, useRef, type PointerEvent } from "react";

export const WORKSPACE_MENU_OPEN = "copilot:workspace-menu-open";

type HoverMenuOptions = {
  onOpen: () => void;
  onClose: () => void;
  keepOpen?: () => boolean;
};

/** Share these handlers between a menu trigger and its panel, including portals. */
export function useHoverMenu(options: HoverMenuOptions) {
  const latest = useRef(options);
  latest.current = options;
  const opening = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const closing = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const cancel = useCallback(() => {
    clearTimeout(opening.current);
    clearTimeout(closing.current);
    opening.current = undefined;
    closing.current = undefined;
  }, []);

  const onPointerEnter = useCallback((event: PointerEvent) => {
    cancel();
    if (event.pointerType === "touch" || event.buttons) return;
    opening.current = setTimeout(() => {
      opening.current = undefined;
      latest.current.onOpen();
    }, 120);
  }, [cancel]);

  const onPointerLeave = useCallback((event: PointerEvent) => {
    cancel();
    if (event.pointerType === "touch" || event.buttons) return;
    closing.current = setTimeout(() => {
      closing.current = undefined;
      if (!latest.current.keepOpen?.()) latest.current.onClose();
    }, 240);
  }, [cancel]);

  useEffect(() => {
    const blur = () => {
      cancel();
      latest.current.onClose();
    };
    window.addEventListener("blur", blur);
    return () => {
      cancel();
      window.removeEventListener("blur", blur);
    };
  }, [cancel]);

  return { onPointerEnter, onPointerLeave, cancel };
}
