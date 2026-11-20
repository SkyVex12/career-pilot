import React, { useContext, useState } from "react";
import {
  Modal,
  Pressable,
  ScrollView,
  View,
  useWindowDimensions,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import Svg, { Polygon } from "react-native-svg";
import {
  Badge,
  Button,
  Icon,
  IconButton,
  Notice,
  Row,
  Segments,
  Theme,
  Txt,
  type IconName,
} from "./ui";
import { HTTP_TEST_BUILD } from "./transport";
export type Profile = { id: string; name: string; me: boolean };
const navigation: {
  label: string;
  short: string;
  tab: string;
  icon: IconName;
  group: string[];
}[] = [
  {
    label: "Call",
    short: "Call",
    tab: "Companion",
    icon: "call",
    group: ["Companion", "In-person"],
  },
  {
    label: "Interview prep",
    short: "Prep",
    tab: "Practice",
    icon: "practice",
    group: ["Practice", "Preparation"],
  },
  { label: "Work", short: "Work", tab: "Ask", icon: "work", group: ["Ask"] },
  {
    label: "History",
    short: "History",
    tab: "History",
    icon: "history",
    group: ["History"],
  },
];
function Brand({ compact = false }: { compact?: boolean }) {
  const c = useContext(Theme);
  return (
    <Row style={{ gap: 10 }}>
      <Svg width={30} height={30} viewBox="0 0 32 32" fill="none" accessible={false}>
        <Polygon fill="#f05a3c" points="9,3 23,3 15,11 1,11" />
        <Polygon fill="#ff7a5c" points="9,3 15,11 1,11" />
        <Polygon fill="#e44832" points="1,12.2 15,12.2 15,28.2" />
        <Polygon fill="#cf3b2e" points="9,12.2 15,12.2 15,28.2" />
        <Polygon fill="#bd352b" points="23.8485,3.8485 31,12 16.2,29.5 16.2,11.4971" />
        <Polygon fill="#d84b35" points="23.8485,3.8485 31,12 16.2,11.4971" />
      </Svg>
      {!compact ? (
        <Txt style={{ fontWeight: "700", fontSize: 17, letterSpacing: -0.3 }}>
          Co<Txt style={{ color: c.ruby, fontWeight: "700", fontSize: 17, letterSpacing: -0.3 }}>Ruby</Txt>
        </Txt>
      ) : null}
    </Row>
  );
}
export { Brand };
export function AppShell({
  profiles,
  profile,
  email,
  server,
  tab,
  error,
  serverNotice,
  onProfile,
  onTab,
  onSignout,
  children,
}: {
  profiles: Profile[];
  profile: string;
  email: string;
  server: string;
  tab: string;
  error?: string;
  serverNotice?: string | null;
  onProfile: (p: Profile) => void;
  onTab: (tab: string) => void;
  onSignout: () => void;
  children: React.ReactNode;
}) {
  const c = useContext(Theme),
    { width, height, fontScale } = useWindowDimensions();
  const shortLandscape = width >= 600 && height < 500 && width > height * 1.2;
  const wide = shortLandscape || (width >= 850 && height >= 460);
  const secondaryOptions = ["Companion", "In-person"].includes(tab)
    ? [
        { value: "Companion", label: "Desktop companion" },
        { value: "In-person", label: "In person" },
      ]
    : ["Practice", "Preparation"].includes(tab)
      ? [
          { value: "Practice", label: "Practice" },
          { value: "Preparation", label: "Meetings" },
        ]
      : null;
  const [sheet, setSheet] = useState<"profiles" | "account" | null>(null);
  const current = profiles.find((p) => (p.me ? "" : p.id) === profile);
  const name = current?.name || (profile ? "Selected profile" : "My profile");
  function nav(rail: boolean) {
    return (
      <View
        accessibilityRole="tablist"
        style={{
          flexDirection: rail ? "column" : "row",
          gap: rail ? (shortLandscape ? 2 : 14) : 2,
          ...(rail
            ? { paddingHorizontal: shortLandscape ? 4 : 10 }
            : {
                padding: 5,
                borderTopWidth: 1,
                borderColor: c.line,
                backgroundColor: c.card,
              }),
        }}
      >
        {navigation.map((item) => {
          const selected = item.group.includes(tab);
          return (
            <Pressable
              key={item.tab}
              accessibilityRole="tab"
              accessibilityLabel={item.label}
              accessibilityState={{ selected }}
              onPress={() => onTab(item.tab)}
              style={({ pressed }) => ({
                ...(rail ? { width: shortLandscape ? 64 : 68 } : { flex: 1 }),
                minHeight: shortLandscape ? 48 : 62,
                borderRadius: 16,
                paddingVertical: shortLandscape ? 4 : 9,
                alignItems: "center",
                justifyContent: "center",
                gap: 4,
                backgroundColor: selected
                  ? c.tint
                  : pressed
                    ? c.soft
                    : "transparent",
              })}
            >
              <Icon
                name={item.icon}
                size={22}
                color={selected ? c.ruby : c.muted}
              />
              <Txt
                style={{
                  fontSize: 11,
                  lineHeight: 16,
                  fontWeight: selected ? "700" : "500",
                  color: selected ? c.ruby : c.muted,
                }}
              >
                {item.short}
              </Txt>
            </Pressable>
          );
        })}
      </View>
    );
  }
  return (
    <View style={{ flex: 1, flexDirection: "row" }}>
      {wide ? (
        <ScrollView
          style={{ width: shortLandscape ? 73 : 89, flexGrow: 0 }}
          contentContainerStyle={{
            flexGrow: 1,
            backgroundColor: c.card,
            borderRightWidth: 1,
            borderColor: c.line,
            paddingTop: shortLandscape ? 8 : 24,
            gap: shortLandscape ? 8 : 34,
            alignItems: "center",
          }}
        >
          {!shortLandscape || height >= 340 ? <Brand compact /> : null}
          {nav(true)}
          <View style={{ flex: 1 }} />
          <IconButton
            label="Account and server"
            icon="settings"
            onPress={() => setSheet("account")}
          />
          <View style={{ height: shortLandscape ? 0 : 16 }} />
        </ScrollView>
      ) : null}
      <View style={{ flex: 1, minWidth: 0 }}>
        <View
          style={{
            minHeight: shortLandscape ? 56 : 72,
            paddingHorizontal: shortLandscape ? 12 : wide ? 26 : 18,
            paddingVertical: shortLandscape ? 4 : 11,
            flexDirection: "row",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            borderBottomWidth: 1,
            borderColor: c.line,
            backgroundColor: c.card,
          }}
        >
          {shortLandscape && secondaryOptions ? (
            <View style={{ flex: 1 }}>
              <Segments
                value={tab}
                options={secondaryOptions}
                onChange={onTab}
              />
            </View>
          ) : wide ? (
            <View style={{ gap: 2 }}>
              <Txt style={{ fontSize: 17, fontWeight: "700" }}>
                {navigation.find((i) => i.group.includes(tab))?.label}
              </Txt>
              {HTTP_TEST_BUILD && !shortLandscape ? (
                <Txt muted style={{ fontSize: 10, lineHeight: 14 }}>
                  HTTP TEST BUILD
                </Txt>
              ) : null}
            </View>
          ) : (
            <Brand compact={width < 400 || fontScale > 1.2} />
          )}
          <View
            style={{
              flexDirection: "row",
              alignItems: "center",
              gap: 3,
              maxWidth: wide ? 280 : "54%",
            }}
          >
            <Pressable
              accessibilityRole="button"
              accessibilityLabel={`Switch profile. Current profile: ${name}`}
              onPress={() => setSheet("profiles")}
              style={({ pressed }) => ({
                flexDirection: "row",
                alignItems: "center",
                gap: 8,
                minHeight: 48,
                borderRadius: 14,
                paddingHorizontal: 9,
                backgroundColor: pressed ? c.soft : "transparent",
                flexShrink: 1,
              })}
            >
              <View
                style={{
                  width: 31,
                  height: 31,
                  borderRadius: 11,
                  backgroundColor: c.tint,
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                <Txt style={{ color: c.ruby, fontSize: 12, fontWeight: "700" }}>
                  {name
                    .split(/\s+/)
                    .slice(0, 2)
                    .map((x) => x[0])
                    .join("")}
                </Txt>
              </View>
              {!shortLandscape ? (
                <Txt
                  numberOfLines={1}
                  style={{
                    maxWidth: wide ? 170 : 105,
                    fontSize: 13,
                    fontWeight: "600",
                  }}
                >
                  {name}
                </Txt>
              ) : null}
              <Icon name="chevron" size={14} />
            </Pressable>
            {!wide ? (
              <IconButton
                label="Account and server"
                icon="more"
                onPress={() => setSheet("account")}
              />
            ) : null}
          </View>
        </View>
        {error ? (
          <View style={{ paddingHorizontal: 20, paddingTop: 10 }}>
            <Notice tone="error">{error}</Notice>
          </View>
        ) : null}
        {secondaryOptions && !shortLandscape ? (
          <View
            style={{
              width: "100%",
              maxWidth: 860,
              alignSelf: "center",
              paddingHorizontal: 22,
              paddingTop: 18,
            }}
          >
            <Segments value={tab} options={secondaryOptions} onChange={onTab} />
          </View>
        ) : null}
        <View style={{ flex: 1, minHeight: 0 }}>{children}</View>
        {!wide ? nav(false) : null}
      </View>
      <Modal
        visible={sheet !== null}
        transparent
        animationType="slide"
        onRequestClose={() => setSheet(null)}
      >
        <View
          style={{
            flex: 1,
            backgroundColor: "rgba(0,0,0,0.4)",
            justifyContent: "flex-end",
          }}
        >
          <Pressable
            accessibilityLabel="Close account panel"
            onPress={() => setSheet(null)}
            style={{ flex: 1 }}
          />
          <SafeAreaView
            edges={["bottom"]}
            accessibilityViewIsModal
            style={{
              maxHeight: "80%",
              width: "100%",
              maxWidth: 600,
              alignSelf: "center",
              borderTopLeftRadius: 26,
              borderTopRightRadius: 26,
              backgroundColor: c.card,
            }}
          >
            <ScrollView contentContainerStyle={{ padding: 24, gap: 16 }}>
              <Row style={{ justifyContent: "space-between" }}>
                <Txt style={{ fontSize: 21, fontWeight: "700", flex: 1 }}>
                  {sheet === "profiles" ? "Choose a profile" : "Your account"}
                </Txt>
                <IconButton
                  label="Close account panel"
                  icon="close"
                  onPress={() => setSheet(null)}
                />
              </Row>
              {sheet === "profiles" ? (
                <>
                  {profiles.length === 0 ? (
                    <Txt muted>
                      No profiles loaded. Reconnect to refresh your account.
                    </Txt>
                  ) : (
                    profiles.map((p) => (
                      <Pressable
                        key={p.id}
                        accessibilityRole="radio"
                        accessibilityState={{
                          selected: (p.me ? "" : p.id) === profile,
                        }}
                        onPress={() => {
                          onProfile(p);
                          setSheet(null);
                        }}
                        style={({ pressed }) => ({
                          minHeight: 58,
                          padding: 15,
                          backgroundColor:
                            (p.me ? "" : p.id) === profile
                              ? c.tint
                              : pressed
                                ? c.soft
                                : "transparent",
                          borderRadius: 14,
                          flexDirection: "row",
                          alignItems: "center",
                          gap: 12,
                        })}
                      >
                        <View style={{ flex: 1 }}>
                          <Txt style={{ fontWeight: "600" }}>
                            {p.name || "My profile"}
                          </Txt>
                          {p.me ? (
                            <Txt muted style={{ fontSize: 12 }}>
                              Your profile
                            </Txt>
                          ) : null}
                        </View>
                        {(p.me ? "" : p.id) === profile ? (
                          <Icon name="check" color={c.ruby} />
                        ) : null}
                      </Pressable>
                    ))
                  )}
                </>
              ) : (
                <>
                  <Txt>{email || "Signed in with HubRuby"}</Txt>
                  <Txt muted selectable style={{ fontSize: 13 }}>
                    {server}
                  </Txt>
                  {HTTP_TEST_BUILD ? (
                    <>
                      <Badge label="HTTP test" tone="warning" />
                      <Notice>
                        HTTP connections send sign-in and call content without
                        encryption. Use a test account and test content.
                      </Notice>
                    </>
                  ) : null}
                  {serverNotice ? <Notice>{serverNotice}</Notice> : null}
                  <Button
                    label="Sign out of this phone"
                    secondary
                    icon="logout"
                    onPress={() => {
                      setSheet(null);
                      onSignout();
                    }}
                  />
                </>
              )}
            </ScrollView>
          </SafeAreaView>
        </View>
      </Modal>
    </View>
  );
}
