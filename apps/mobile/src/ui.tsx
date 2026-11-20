import React, { createContext, useContext, useMemo, useState } from "react";
import {
  Pressable,
  Text as NativeText,
  TextInput,
  View,
  ActivityIndicator,
  Modal,
  ScrollView,
  FlatList,
  KeyboardAvoidingView,
  Platform,
  useWindowDimensions,
  type TextProps,
  type TextInputProps,
  type ViewStyle,
  type StyleProp,
  type ScrollViewProps,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import Svg, { Path } from "react-native-svg";

export function colors(dark: boolean) {
  return dark
    ? {
        bg: "#141419",
        card: "#202027",
        soft: "#2b2932",
        text: "#f7f5f7",
        muted: "#aaa5b2",
        line: "#35323e",
        ruby: "#e66a8a",
        onRuby: "#211118",
        tint: "#3e2532",
        success: "#77cfac",
        warning: "#efc27b",
        danger: "#ff98a6",
      }
    : {
        bg: "#f5f5f8",
        card: "#ffffff",
        soft: "#eeeef3",
        text: "#232330",
        muted: "#6d6c7e",
        line: "#e5e4ec",
        ruby: "#a52c52",
        onRuby: "#ffffff",
        tint: "#faeaf0",
        success: "#197b59",
        warning: "#8e5a12",
        danger: "#b52b43",
      };
}
export const Theme = createContext(colors(false));
export type IconName =
  | "call"
  | "practice"
  | "work"
  | "history"
  | "chevron"
  | "close"
  | "arrow"
  | "refresh"
  | "mic"
  | "headphones"
  | "check"
  | "plus"
  | "settings"
  | "search"
  | "logout"
  | "screen"
  | "more"
  | "file"
  | "calendar"
  | "stop";
const paths: Record<IconName, string> = {
  call: "M4 9v6m4-10v14m4-12v10m4-14v18m4-12v6",
  practice: "M4 5h16v12H9l-5 4V5Zm4 4h8m-8 4h5",
  work: "M8 6V4h8v2M3 7h18v13H3V7Zm0 5c6 4 12 4 18 0m-9 0v4",
  history: "M3 10a9 9 0 1 1 2 8M3 4v6h6m3-4v6l4 2",
  chevron: "m8 10 4 4 4-4",
  close: "m6 6 12 12M6 18 18 6",
  arrow: "M4 12h16m-6-6 6 6-6 6",
  refresh: "M20 9A8 8 0 0 0 6 6L3 9m0-6v6h6M4 15a8 8 0 0 0 14 3l3-3m0 6v-6h-6",
  mic: "M9 5a3 3 0 0 1 6 0v7a3 3 0 0 1-6 0V5Zm-3 6v1a6 6 0 0 0 12 0v-1m-6 7v4m-3 0h6",
  headphones: "M3 14v-3a9 9 0 0 1 18 0v3M3 13h4v8H3v-8Zm14 0h4v8h-4v-8Z",
  check: "m5 12 4 4L19 6",
  plus: "M12 4v16M4 12h16",
  settings: "M4 7h16M4 17h16M8 4v6m8 4v6",
  search: "M16 16 21 21M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z",
  logout: "M9 4H4v16h5m4-13 5 5-5 5M8 12h12",
  screen: "M3 4h18v13H3V4Zm9 13v4m-4 0h8",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  file: "M14 3H5v18h14V8l-5-5Zm0 0v6h5M8 13h8m-8 4h6",
  calendar: "M4 5h16v16H4V5Zm0 5h16M8 3v4m8-4v4m-8 7h3m2 0h3m-8 4h3",
  stop: "M6 6h12v12H6V6Z",
};
export function Icon({
  name,
  size = 22,
  color,
}: {
  name: IconName;
  size?: number;
  color?: string;
}) {
  const c = useContext(Theme);
  return (
    <Svg width={size} height={size} viewBox="0 0 24 24" accessible={false}>
      <Path
        d={paths[name]}
        fill="none"
        stroke={color ?? c.muted}
        strokeWidth={1.8}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </Svg>
  );
}
export function Txt({
  muted,
  title,
  ...p
}: TextProps & { muted?: boolean; title?: boolean }) {
  const c = useContext(Theme);
  return (
    <NativeText
      {...p}
      accessibilityRole={p.accessibilityRole ?? (title ? "header" : undefined)}
      style={[
        {
          color: muted ? c.muted : c.text,
          fontSize: title ? 27 : 15,
          lineHeight: title ? 34 : 23,
          ...(title ? { fontWeight: "700", letterSpacing: -0.65 } : {}),
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
  loading = false,
  icon,
  compact = false,
  danger = false,
  style,
}: {
  label: string;
  onPress: () => void;
  disabled?: boolean;
  secondary?: boolean;
  loading?: boolean;
  icon?: IconName;
  compact?: boolean;
  danger?: boolean;
  style?: StyleProp<ViewStyle>;
}) {
  const c = useContext(Theme);
  const foreground = secondary ? (danger ? c.danger : c.text) : c.onRuby;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityState={{ disabled: disabled || loading, busy: loading }}
      disabled={disabled || loading}
      onPress={onPress}
      style={({ pressed }) => [
        {
          minHeight: 48,
          borderRadius: 14,
          paddingHorizontal: compact ? 12 : 18,
          paddingVertical: 11,
          flexDirection: "row",
          gap: 9,
          alignItems: "center",
          justifyContent: "center",
          backgroundColor: secondary ? c.card : danger ? c.danger : c.ruby,
          borderWidth: secondary ? 1 : 0,
          borderColor: c.line,
          opacity: disabled ? 0.48 : pressed ? 0.72 : 1,
        },
        style,
      ]}
    >
      {loading ? (
        <ActivityIndicator size="small" color={foreground} />
      ) : icon ? (
        <Icon name={icon} size={18} color={foreground} />
      ) : null}
      <Txt
        style={{
          color: foreground,
          fontWeight: "600",
          fontSize: 14,
          textAlign: "center",
          flexShrink: 1,
        }}
      >
        {label}
      </Txt>
    </Pressable>
  );
}
export function IconButton({
  label,
  icon,
  onPress,
  disabled = false,
}: {
  label: string;
  icon: IconName;
  onPress: () => void;
  disabled?: boolean;
}) {
  const c = useContext(Theme);
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityState={{ disabled }}
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => ({
        height: 48,
        width: 48,
        borderRadius: 14,
        alignItems: "center",
        justifyContent: "center",
        backgroundColor: pressed ? c.soft : "transparent",
        opacity: disabled ? 0.4 : 1,
      })}
    >
      <Icon name={icon} />
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
          minHeight: 50,
          borderWidth: 1,
          borderColor: c.line,
          borderRadius: 14,
          color: c.text,
          backgroundColor: c.card,
          paddingHorizontal: 15,
          paddingVertical: 13,
          fontSize: 15,
          textAlignVertical: props.multiline ? "top" : "center",
        },
        props.style,
      ]}
    />
  );
}
export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: React.ReactNode;
  hint?: string;
}) {
  return (
    <View style={{ gap: 7 }}>
      <Txt style={{ fontSize: 13, fontWeight: "600" }}>{label}</Txt>
      {children}
      {hint ? (
        <Txt muted style={{ fontSize: 12, lineHeight: 18 }}>
          {hint}
        </Txt>
      ) : null}
    </View>
  );
}
export function Card({
  children,
  style,
  subtle = false,
}: {
  children: React.ReactNode;
  style?: StyleProp<ViewStyle>;
  subtle?: boolean;
}) {
  const c = useContext(Theme);
  return (
    <View
      style={[
        {
          backgroundColor: subtle ? c.soft : c.card,
          borderWidth: 1,
          borderColor: c.line,
          borderRadius: 20,
          padding: 20,
          gap: 14,
        },
        style,
      ]}
    >
      {children}
    </View>
  );
}
export function Row({
  children,
  style,
}: {
  children: React.ReactNode;
  style?: StyleProp<ViewStyle>;
}) {
  return (
    <View
      style={[
        {
          flexDirection: "row",
          gap: 8,
          flexWrap: "wrap",
          alignItems: "center",
        },
        style,
      ]}
    >
      {children}
    </View>
  );
}
export function Loading({ label = "Loading…" }: { label?: string }) {
  const c = useContext(Theme);
  return (
    <View
      style={{
        flexDirection: "row",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
        gap: 10,
      }}
    >
      <ActivityIndicator accessibilityLabel={label} color={c.ruby} />
      <Txt muted>{label}</Txt>
    </View>
  );
}
export const page: ViewStyle = {
  padding: 22,
  gap: 18,
  paddingBottom: 32,
  width: "100%",
  maxWidth: 860,
  alignSelf: "center",
};
export function Screen({ children, ...props }: ScrollViewProps) {
  return (
    <ScrollView
      keyboardShouldPersistTaps="handled"
      {...props}
      contentContainerStyle={[page, props.contentContainerStyle]}
    >
      {children}
    </ScrollView>
  );
}
export function ScreenTitle({
  title,
  subtitle,
  eyebrow,
  action,
}: {
  title: string;
  subtitle?: string;
  eyebrow?: string;
  action?: React.ReactNode;
}) {
  const c = useContext(Theme);
  return (
    <View style={{ gap: 7, paddingVertical: 4 }}>
      <View
        style={{
          flexDirection: "row",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 12,
        }}
      >
        <View style={{ flex: 1, gap: 5 }}>
          {eyebrow ? (
            <Txt
              style={{
                fontSize: 11,
                letterSpacing: 1.4,
                fontWeight: "700",
                color: c.ruby,
              }}
            >
              {eyebrow.toUpperCase()}
            </Txt>
          ) : null}
          <Txt title>{title}</Txt>
        </View>
        {action}
      </View>
      {subtitle ? <Txt muted>{subtitle}</Txt> : null}
    </View>
  );
}
export function Notice({
  children,
  tone = "info",
  action,
}: {
  children: React.ReactNode;
  tone?: "info" | "error" | "success";
  action?: React.ReactNode;
}) {
  const c = useContext(Theme);
  return (
    <View
      accessibilityRole={tone === "error" ? "alert" : undefined}
      style={{
        backgroundColor: c.card,
        borderColor: tone === "error" ? c.danger : c.line,
        borderWidth: 1,
        borderRadius: 14,
        padding: 14,
        gap: 10,
      }}
    >
      <Txt
        style={{
          fontSize: 13,
          lineHeight: 20,
          color:
            tone === "error"
              ? c.danger
              : tone === "success"
                ? c.success
                : c.muted,
        }}
      >
        {children}
      </Txt>
      {action}
    </View>
  );
}
export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon: IconName;
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  const c = useContext(Theme);
  return (
    <Card style={{ alignItems: "center", paddingVertical: 32 }}>
      <View style={{ backgroundColor: c.tint, padding: 18, borderRadius: 24 }}>
        <Icon name={icon} size={30} color={c.ruby} />
      </View>
      <Txt style={{ fontSize: 19, fontWeight: "600", textAlign: "center" }}>
        {title}
      </Txt>
      <Txt muted style={{ maxWidth: 390, textAlign: "center" }}>
        {description}
      </Txt>
      {action}
    </Card>
  );
}
export function Badge({
  label,
  tone = "neutral",
}: {
  label: string;
  tone?: "neutral" | "success" | "ruby" | "warning";
}) {
  const c = useContext(Theme);
  return (
    <View
      style={{
        alignSelf: "flex-start",
        backgroundColor: tone === "ruby" ? c.tint : c.soft,
        borderRadius: 7,
        paddingHorizontal: 8,
        paddingVertical: 3,
      }}
    >
      <Txt
        style={{
          fontSize: 11,
          lineHeight: 16,
          fontWeight: "600",
          color:
            tone === "success"
              ? c.success
              : tone === "ruby"
                ? c.ruby
                : tone === "warning"
                  ? c.warning
                  : c.muted,
        }}
      >
        {label}
      </Txt>
    </View>
  );
}
export type Choice = { value: string; label: string; description?: string };
export function Select({
  label,
  value,
  options,
  onChange,
  placeholder = "Choose…",
  disabled = false,
}: {
  label: string;
  value: string;
  options: Choice[];
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
}) {
  const c = useContext(Theme);
  const { width, height } = useWindowDimensions();
  const compact = width > height || height < 500;
  const [open, setOpen] = useState(false),
    [query, setQuery] = useState("");
  const selected = options.find((o) => o.value === value);
  const filtered = useMemo(
    () =>
      options.filter((o) =>
        `${o.label} ${o.description ?? ""}`
          .toLocaleLowerCase()
          .includes(query.toLocaleLowerCase()),
      ),
    [options, query],
  );
  return (
    <>
      <Field label={label}>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={`${label}: ${selected?.label ?? placeholder}`}
          accessibilityState={{ disabled, expanded: open }}
          disabled={disabled}
          onPress={() => {
            setQuery("");
            setOpen(true);
          }}
          style={({ pressed }) => ({
            minHeight: 52,
            borderWidth: 1,
            borderColor: c.line,
            borderRadius: 14,
            backgroundColor: pressed ? c.soft : c.card,
            padding: 14,
            flexDirection: "row",
            alignItems: "center",
            gap: 12,
            opacity: disabled ? 0.5 : 1,
          })}
        >
          <Txt style={{ flex: 1 }} numberOfLines={2}>
            {selected?.label ?? placeholder}
          </Txt>
          <Icon name="chevron" size={18} />
        </Pressable>
      </Field>
      <Modal
        visible={open}
        transparent
        animationType="slide"
        onRequestClose={() => setOpen(false)}
      >
        <KeyboardAvoidingView
          behavior={Platform.OS === "ios" ? "padding" : undefined}
          style={{
            flex: 1,
            justifyContent: "flex-end",
            backgroundColor: "rgba(0,0,0,0.4)",
          }}
        >
          <Pressable
            accessibilityLabel="Dismiss selection"
            onPress={() => setOpen(false)}
            style={{ flex: 1 }}
          />
          <SafeAreaView
            edges={["bottom"]}
            accessibilityViewIsModal
            style={{
              maxHeight: compact ? "96%" : "82%",
              minHeight: 0,
              flexShrink: 1,
              backgroundColor: c.card,
              borderTopLeftRadius: 26,
              borderTopRightRadius: 26,
              width: "100%",
              maxWidth: 650,
              alignSelf: "center",
            }}
          >
            <View
              style={{
                paddingHorizontal: compact ? 12 : 20,
                paddingVertical: compact ? 4 : 10,
              }}
            >
              <Row style={{ justifyContent: "space-between" }}>
                <Txt
                  numberOfLines={compact ? 1 : 2}
                  style={{ fontWeight: "700", fontSize: 20, flex: 1 }}
                >
                  {label}
                </Txt>
                <IconButton
                  label="Close selection"
                  icon="close"
                  onPress={() => setOpen(false)}
                />
              </Row>
            </View>
            <FlatList
              style={{ minHeight: 0, flexShrink: 1 }}
              keyboardDismissMode={
                Platform.OS === "ios" ? "interactive" : "on-drag"
              }
              ListHeaderComponent={
                options.length > 7 ? (
                  <View style={{ paddingBottom: compact ? 6 : 12 }}>
                    <Input
                      placeholder={`Search ${label.toLowerCase()}`}
                      value={query}
                      onChangeText={setQuery}
                      autoCorrect={false}
                    />
                  </View>
                ) : undefined
              }
              data={filtered}
              keyExtractor={(item) => item.value}
              keyboardShouldPersistTaps="handled"
              contentContainerStyle={{
                paddingHorizontal: 20,
                paddingBottom: 24,
              }}
              ListEmptyComponent={
                <Txt muted style={{ padding: 16 }}>
                  No matching options.
                </Txt>
              }
              renderItem={({ item }) => (
                <Pressable
                  accessibilityRole="radio"
                  accessibilityState={{ selected: item.value === value }}
                  onPress={() => {
                    onChange(item.value);
                    setOpen(false);
                  }}
                  style={({ pressed }) => ({
                    minHeight: 56,
                    paddingVertical: 13,
                    paddingHorizontal: 12,
                    marginBottom: 4,
                    borderRadius: 12,
                    backgroundColor:
                      item.value === value
                        ? c.tint
                        : pressed
                          ? c.soft
                          : "transparent",
                    flexDirection: "row",
                    gap: 12,
                    alignItems: "center",
                  })}
                >
                  <View style={{ flex: 1 }}>
                    <Txt
                      style={{
                        fontWeight: item.value === value ? "600" : "400",
                      }}
                    >
                      {item.label}
                    </Txt>
                    {item.description ? (
                      <Txt muted style={{ fontSize: 12 }}>
                        {item.description}
                      </Txt>
                    ) : null}
                  </View>
                  {item.value === value ? (
                    <Icon name="check" color={c.ruby} />
                  ) : null}
                </Pressable>
              )}
            />
          </SafeAreaView>
        </KeyboardAvoidingView>
      </Modal>
    </>
  );
}
export function Segments({
  value,
  options,
  onChange,
  disabled = false,
}: {
  value: string;
  options: Choice[];
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  const c = useContext(Theme);
  return (
    <View
      accessibilityRole="tablist"
      style={{
        backgroundColor: c.soft,
        borderRadius: 13,
        padding: 4,
        flexDirection: "row",
        gap: 3,
      }}
    >
      {options.map((o) => (
        <Pressable
          key={o.value}
          accessibilityRole="tab"
          accessibilityState={{ selected: value === o.value, disabled }}
          disabled={disabled}
          onPress={() => onChange(o.value)}
          style={({ pressed }) => ({
            flex: 1,
            minHeight: 44,
            borderRadius: 10,
            padding: 8,
            justifyContent: "center",
            backgroundColor: value === o.value ? c.card : "transparent",
            opacity: pressed ? 0.7 : 1,
          })}
        >
          <Txt
            style={{
              fontSize: 13,
              fontWeight: value === o.value ? "600" : "400",
              color: value === o.value ? c.text : c.muted,
              textAlign: "center",
            }}
          >
            {o.label}
          </Txt>
        </Pressable>
      ))}
    </View>
  );
}
