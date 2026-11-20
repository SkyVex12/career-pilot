import React, { createContext, useContext } from "react";
import {
  Pressable,
  Text as NativeText,
  TextInput,
  View,
  StyleSheet,
  useColorScheme,
  ActivityIndicator,
  type TextProps,
  type TextInputProps,
} from "react-native";
export function colors(dark: boolean) {
  return dark
    ? {
        bg: "#171418",
        card: "#252027",
        text: "#f8f2f6",
        muted: "#c0b0ba",
        line: "#463844",
        ruby: "#b82f59",
      }
    : {
        bg: "#faf7f8",
        card: "#ffffff",
        text: "#291b24",
        muted: "#766570",
        line: "#eadde3",
        ruby: "#a7214b",
      };
}
export const Theme = createContext(colors(false));
export function Txt(p: TextProps & { muted?: boolean; title?: boolean }) {
  const c = useContext(Theme);
  return (
    <NativeText
      {...p}
      accessibilityRole={
        p.accessibilityRole ?? (p.title ? "header" : undefined)
      }
      style={[
        {
          color: p.muted ? c.muted : c.text,
          fontSize: p.title ? 24 : 16,
          lineHeight: p.title ? 31 : 24,
        },
        p.style,
      ]}
    />
  );
}
export function Button({
  label,
  onPress,
  disabled = false,
  secondary = false,
}: {
  label: string;
  onPress: () => void;
  disabled?: boolean;
  secondary?: boolean;
}) {
  const c = useContext(Theme);
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => ({
        minHeight: 48,
        borderRadius: 10,
        paddingHorizontal: 16,
        paddingVertical: 10,
        alignItems: "center",
        justifyContent: "center",
        backgroundColor: secondary ? c.card : c.ruby,
        borderWidth: secondary ? 1 : 0,
        borderColor: c.line,
        opacity: disabled ? 0.45 : pressed ? 0.75 : 1,
      })}
    >
      <Txt style={{ color: secondary ? c.text : "#fff", fontWeight: "600" }}>
        {label}
      </Txt>
    </Pressable>
  );
}
export function Input(props: TextInputProps) {
  const c = useContext(Theme);
  return (
    <TextInput
      {...props}
      accessibilityLabel={props.accessibilityLabel ?? props.placeholder}
      placeholderTextColor={c.muted}
      style={[
        {
          minHeight: 48,
          borderWidth: 1,
          borderColor: c.line,
          borderRadius: 10,
          color: c.text,
          backgroundColor: c.card,
          padding: 12,
          fontSize: 16,
          textAlignVertical: "top",
        },
        props.style,
      ]}
    />
  );
}
export function Card({ children }: { children: React.ReactNode }) {
  const c = useContext(Theme);
  return (
    <View
      style={{
        backgroundColor: c.card,
        borderWidth: 1,
        borderColor: c.line,
        borderRadius: 14,
        padding: 16,
        gap: 10,
      }}
    >
      {children}
    </View>
  );
}
export function Row({ children }: { children: React.ReactNode }) {
  return (
    <View style={{ flexDirection: "row", gap: 8, flexWrap: "wrap" }}>
      {children}
    </View>
  );
}
export function Loading() {
  const c = useContext(Theme);
  return <ActivityIndicator accessibilityLabel="Loading" color={c.ruby} />;
}
export const page = { padding: 16, gap: 14, paddingBottom: 40 };
