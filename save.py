"""
Example with automatic database updates and video saving
Loads workstations from DB, updates metrics every 10 seconds,
and saves the annotated output to a video file.
"""

from workstation_inference import WorkstationInference, DatabaseConfig
import cv2
import os

def main():
    # =====================
    # CONFIGURATION
    # =====================
    VIDEO_PATH = "day_2.avi"  # or 0 for webcam
    OUTPUT_PATH = "output_processed.mp4" # Path to save the new video
    MODEL_PATH = "yolov8s.pt"
    
    # Database configuration
    DB_CONFIG = DatabaseConfig(
        host="localhost",
        port=5432,
        dbname="test",
        user="postgres",
        password="admin123"
    )
    
    # Organization and Camera IDs
    ORG_ID = 1
    CAM_ID = 1
    
    # =====================
    # SETUP
    # =====================
    print("🔧 Initializing inference system...")
    print("   - Loading workstations from database")
    print("   - Auto-update enabled (every 10 seconds)")
    print(f"   - Output will be saved to: {OUTPUT_PATH}\n")
    
    # Create inference instance
    inference = WorkstationInference(
        model_path=MODEL_PATH,
        confidence_threshold=0.4,
        missing_threshold=3.0,
        db_config=DB_CONFIG,
        org_id=ORG_ID,
        cam_id=CAM_ID,
        db_update_interval=10.0,  
        auto_update_db=True        
    )
    
    # Check if workstations were loaded
    if not inference.workstations:
        print("❌ No workstations found in database")
        print("   Please add workstations to the 'workstations' table:")
        print("   INSERT INTO workstations (org_id, cam_id, name, x1, y1, x2, y2)")
        print("   VALUES (1, 1, 'Desk-A', 0.1, 0.15, 0.4, 0.7);")
        return
    
    # Open video source
    cap = cv2.VideoCapture(VIDEO_PATH)
    
    if not cap.isOpened():
        print("❌ Error: Could not open video source")
        return
    
    # Get video properties for the VideoWriter
    fps = cap.get(cv2.CAP_PROP_FPS)
    # Fallback to 30 FPS if webcam/source doesn't provide it
    if fps == 0 or fps != fps: 
        fps = 30.0
        
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    
    # Setup VideoWriter
    # 'mp4v' is a good cross-platform codec for .mp4 files
    fourcc = cv2.VideoWriter_fourcc(*'mp4v') 
    out = cv2.VideoWriter(OUTPUT_PATH, fourcc, fps, (width, height))
    
    print(f"📹 Source Video: {width}x{height} @ {fps:.1f} FPS")
    print(f"🎯 Monitoring {len(inference.workstations)} workstations:")
    for name in inference.workstations.keys():
        print(f"   • {name}")
    print()
    print("🚀 Starting inference and recording...")
    print("   Press ESC to quit")
    print("   Press M for metrics")
    print()
    
    # =====================
    # MAIN LOOP
    # =====================
    frame_count = 0
    
    try:
        while cap.isOpened():
            ret, frame = cap.read()
            
            if not ret:
                print("\n✅ Video finished")
                break
            
            # Process frame
            annotated_frame = inference.process_frame(frame)
            
            # Write the annotated frame to the output file
            out.write(annotated_frame)
            
            # Optional: Keep displaying the frame
            cv2.imshow("Workstation Monitoring", annotated_frame)
            
            frame_count += 1
            
            # Status update every 100 frames
            if frame_count % 100 == 0:
                active = sum(1 for ws in inference.workstations.values() if ws.status == "ACTIVE")
                print(f"📊 Frame {frame_count}: {active}/{len(inference.workstations)} active")
            
            # Handle keyboard input
            key = cv2.waitKey(1) & 0xFF
            if key == 27:  # ESC
                print("\n⏹️  Stopped by user")
                break
            elif key == ord('m') or key == ord('M'):
                inference.print_metrics()
    
    except KeyboardInterrupt:
        print("\n⏹️  Interrupted by user")
    
    finally:
        # =====================
        # CLEANUP & FINAL REPORT
        # =====================
        print("\n" + "="*80)
        print("FINAL REPORT")
        print("="*80)
        
        inference.print_metrics()
        
        print(f"\nTotal frames processed: {frame_count}")
        print(f"Video saved successfully to: {OUTPUT_PATH}")
        print("="*80 + "\n")
        
        # Release all resources
        cap.release()
        out.release() # CRITICAL: Release the VideoWriter to finalize the file
        cv2.destroyAllWindows()

if __name__ == "__main__":
    main()