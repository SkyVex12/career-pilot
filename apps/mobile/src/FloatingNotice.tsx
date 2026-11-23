import React, { useContext, useEffect, useRef } from "react";
import { View } from "react-native";
import { Icon, IconButton, Theme, Txt } from "./ui";

/** Transient feedback stays outside the scrolling page and never shifts its controls. */
export function FloatingNotice({
  message,
  onDismiss,
}: {
  message: string;
  onDismiss: () => void;
}) {
  const colors = useContext(Theme);
  const dismiss = useRef(onDismiss);
  dismiss.current = onDismiss;
  useEffect(() => {
    if (!message) return;
    const timer = setTimeout(() => dismiss.current(), 5000);
    return () => clearTimeout(timer);
  }, [message]);
  if (!message) return null;
  return (
    <View
      pointerEvents="box-none"
      style={{
        position: "absolute",
        bottom: 12,
        left: 12,
        right: 12,
        zIndex: 30,
        alignItems: "flex-end",
      }}
    >
      <View
        accessibilityLiveRegion="polite"
        style={{
          width: "100%",
          maxWidth: 400,
          flexDirection: "row",
          alignItems: "center",
          gap: 10,
          paddingLeft: 16,
          paddingVertical: 6,
          paddingRight: 4,
          backgroundColor: colors.card,
          borderColor: colors.line,
          borderWidth: 1,
          borderRadius: 16,
          elevation: 7,
          shadowColor: "#000",
          shadowOpacity: 0.15,
          shadowRadius: 16,
          shadowOffset: { width: 0, height: 6 },
        }}
      >
        <Icon name="check" color={colors.success} size={20} />
        <Txt style={{ flex: 1, fontSize: 13, lineHeight: 20 }}>{message}</Txt>
        <IconButton
          label="Dismiss notification"
          icon="close"
          onPress={onDismiss}
        />
      </View>
    </View>
  );
}
