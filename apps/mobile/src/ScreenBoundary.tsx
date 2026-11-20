import React from "react";
import { View } from "react-native";
import { Button, EmptyState } from "./ui";
export class ScreenBoundary extends React.Component<
  { children: React.ReactNode },
  { failed: boolean; attempt: number }
> {
  state = { failed: false, attempt: 0 };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // Component names aid native diagnostics without logging account content or tokens.
    console.warn("Mobile screen error", error.name, info.componentStack);
  }
  render() {
    if (this.state.failed)
      return (
        <View style={{ flex: 1, justifyContent: "center", padding: 24 }}>
          <EmptyState
            icon="refresh"
            title="This view needs a refresh"
            description="Your saved information is still on the server. Reopen this view to try again."
            action={
              <Button
                label="Reload view"
                icon="refresh"
                onPress={() =>
                  this.setState((s) => ({
                    failed: false,
                    attempt: s.attempt + 1,
                  }))
                }
              />
            }
          />
        </View>
      );
    return (
      <View key={this.state.attempt} style={{ flex: 1 }}>
        {this.props.children}
      </View>
    );
  }
}
