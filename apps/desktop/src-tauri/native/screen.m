// ScreenCaptureKit bridge: one user-selected display/window, excluding our own app.
#import <AppKit/AppKit.h>
#import <ScreenCaptureKit/ScreenCaptureKit.h>
#import <CoreImage/CoreImage.h>
#import <CoreMedia/CoreMedia.h>
#import <ImageIO/ImageIO.h>
#include <unistd.h>
typedef void (*FrameFn)(uint64_t, const unsigned char *, size_t, uint32_t, uint32_t);
typedef void (*ErrorFn)(uint64_t, const char *);
@interface CopilotScreen : NSObject <SCStreamOutput, SCStreamDelegate>
@property (strong) SCStream *stream;
@property (strong) dispatch_queue_t queue;
@property (strong) CIContext *context;
@property uint64_t token;
@property FrameFn frame;
@property ErrorFn error;
@end
@implementation CopilotScreen
- (void)stream:(SCStream *)stream didStopWithError:(NSError *)error {
    self.error(self.token, error.localizedDescription.UTF8String);
}
- (void)stream:(SCStream *)stream didOutputSampleBuffer:(CMSampleBufferRef)sample ofType:(SCStreamOutputType)type {
    if (type != SCStreamOutputTypeScreen || !CMSampleBufferIsValid(sample)) return;
    CFArrayRef attachments = CMSampleBufferGetSampleAttachmentsArray(sample, false);
    if (attachments && CFArrayGetCount(attachments) > 0) {
        NSDictionary *info = (__bridge NSDictionary *)CFArrayGetValueAtIndex(attachments, 0);
        NSNumber *status = info[SCStreamFrameInfoStatus];
        if (status && status.integerValue != SCFrameStatusComplete) return;
    }
    CVImageBufferRef buffer = CMSampleBufferGetImageBuffer(sample);
    if (!buffer) return;
    @autoreleasepool {
        CIImage *image = [CIImage imageWithCVPixelBuffer:buffer];
        CGColorSpaceRef color = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
        NSData *jpeg = [self.context JPEGRepresentationOfImage:image colorSpace:color options:@{(__bridge NSString *)kCGImageDestinationLossyCompressionQuality: @0.85}];
        CGColorSpaceRelease(color);
        if (jpeg) self.frame(self.token, jpeg.bytes, jpeg.length, (uint32_t)CVPixelBufferGetWidth(buffer), (uint32_t)CVPixelBufferGetHeight(buffer));
    }
}
@end
// Called on a Rust worker, never the main thread: ScreenCaptureKit completions must run.
void *copilot_screen_start(uint32_t identifier, bool window, uint64_t token, FrameFn frame, ErrorFn error) {
    @autoreleasepool {
        __block SCShareableContent *content = nil;
        __block NSError *failure = nil;
        dispatch_semaphore_t ready = dispatch_semaphore_create(0);
        [SCShareableContent getShareableContentExcludingDesktopWindows:YES onScreenWindowsOnly:YES completionHandler:^(SCShareableContent *value, NSError *err) {
            content = value; failure = err; dispatch_semaphore_signal(ready);
        }];
        dispatch_semaphore_wait(ready, DISPATCH_TIME_FOREVER);
        if (!content) { error(token, (failure.localizedDescription ?: @"Screen Recording permission is required.").UTF8String); return NULL; }
        SCContentFilter *filter = nil;
        CGFloat width = 0, height = 0;
        if (window) {
            for (SCWindow *item in content.windows) {
                if (item.windowID == identifier && item.owningApplication.processID != getpid()) {
                    filter = [[SCContentFilter alloc] initWithDesktopIndependentWindow:item];
                    width = item.frame.size.width * 2; height = item.frame.size.height * 2;
                    break;
                }
            }
        } else {
            NSMutableArray<SCRunningApplication *> *excluded = [NSMutableArray array];
            for (SCRunningApplication *app in content.applications) if (app.processID == getpid()) [excluded addObject:app];
            for (SCDisplay *display in content.displays) {
                if (display.displayID == identifier) {
                    filter = [[SCContentFilter alloc] initWithDisplay:display excludingApplications:excluded exceptingWindows:@[]];
                    width = display.width; height = display.height; break;
                }
            }
        }
        if (!filter || width <= 0 || height <= 0) { error(token, "The selected screen or window is no longer available."); return NULL; }
        CGFloat scale = MIN(1.0, MIN(1920.0 / width, 1080.0 / height));
        SCStreamConfiguration *config = [SCStreamConfiguration new];
        config.width = MAX(2, ((NSUInteger)(width * scale) / 2) * 2);
        config.height = MAX(2, ((NSUInteger)(height * scale) / 2) * 2);
        config.minimumFrameInterval = CMTimeMake(1, 60);
        config.queueDepth = 3;
        config.showsCursor = YES;
        config.capturesAudio = NO;
        config.pixelFormat = kCVPixelFormatType_32BGRA;
        CopilotScreen *owner = [CopilotScreen new];
        owner.token = token; owner.frame = frame; owner.error = error;
        owner.context = [CIContext contextWithOptions:@{kCIContextUseSoftwareRenderer: @NO}];
        owner.queue = dispatch_queue_create("com.copilot.screen.frames", DISPATCH_QUEUE_SERIAL);
        owner.stream = [[SCStream alloc] initWithFilter:filter configuration:config delegate:owner];
        if (![owner.stream addStreamOutput:owner type:SCStreamOutputTypeScreen sampleHandlerQueue:owner.queue error:&failure]) {
            error(token, failure.localizedDescription.UTF8String); return NULL;
        }
        [owner.stream startCaptureWithCompletionHandler:^(NSError *err) { failure = err; dispatch_semaphore_signal(ready); }];
        dispatch_semaphore_wait(ready, DISPATCH_TIME_FOREVER);
        if (failure) { error(token, failure.localizedDescription.UTF8String); return NULL; }
        return (__bridge_retained void *)owner;
    }
}
void copilot_screen_stop(void *handle) {
    @autoreleasepool {
        CopilotScreen *owner = (__bridge_transfer CopilotScreen *)handle;
        dispatch_semaphore_t stopped = dispatch_semaphore_create(0);
        [owner.stream stopCaptureWithCompletionHandler:^(NSError *error) { dispatch_semaphore_signal(stopped); }];
        dispatch_semaphore_wait(stopped, DISPATCH_TIME_FOREVER);
        [owner.stream removeStreamOutput:owner type:SCStreamOutputTypeScreen error:nil];
        dispatch_sync(owner.queue, ^{});
        owner.stream = nil;
    }
}

// One-shot window capture also uses ScreenCaptureKit, replacing removed legacy CG capture.
typedef void (*SnapshotFn)(void *, const unsigned char *, size_t, const char *);
void copilot_window_snapshot(uint32_t identifier, void *context, SnapshotFn callback) {
    @autoreleasepool {
        dispatch_semaphore_t ready = dispatch_semaphore_create(0);
        __block SCShareableContent *content = nil;
        __block NSError *failure = nil;
        [SCShareableContent getShareableContentExcludingDesktopWindows:YES onScreenWindowsOnly:YES completionHandler:^(SCShareableContent *value, NSError *err) {
            content = value; failure = err; dispatch_semaphore_signal(ready);
        }];
        dispatch_semaphore_wait(ready, DISPATCH_TIME_FOREVER);
        SCWindow *selected = nil;
        for (SCWindow *window in content.windows) if (window.windowID == identifier && window.owningApplication.processID != getpid()) { selected = window; break; }
        if (!selected) { callback(context, NULL, 0, (failure.localizedDescription ?: @"The selected window is no longer available.").UTF8String); return; }
        SCContentFilter *filter = [[SCContentFilter alloc] initWithDesktopIndependentWindow:selected];
        SCStreamConfiguration *config = [SCStreamConfiguration new];
        CGFloat width = selected.frame.size.width * 2, height = selected.frame.size.height * 2;
        CGFloat scale = MIN(1.0, 1568.0 / MAX(width, height));
        config.width = MAX(1, (NSUInteger)(width * scale));
        config.height = MAX(1, (NSUInteger)(height * scale));
        config.showsCursor = NO;
        [SCScreenshotManager captureImageWithFilter:filter configuration:config completionHandler:^(CGImageRef image, NSError *error) {
            if (!image) callback(context, NULL, 0, (error.localizedDescription ?: @"Window capture failed.").UTF8String);
            else {
                NSBitmapImageRep *bitmap = [[NSBitmapImageRep alloc] initWithCGImage:image];
                NSData *png = [bitmap representationUsingType:NSBitmapImageFileTypePNG properties:@{}];
                if (png) callback(context, png.bytes, png.length, NULL);
                else callback(context, NULL, 0, "Could not encode the screenshot.");
            }
            dispatch_semaphore_signal(ready);
        }];
        dispatch_semaphore_wait(ready, DISPATCH_TIME_FOREVER);
    }
}
