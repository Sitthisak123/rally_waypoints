# แผนการพัฒนา VoiceHUD - PackPoint (Implementation Plan)

อ้างอิงจากข้อกำหนดใน `VoiceHUD - PackPoint.txt` และผลการ Reverse-Engineering ระบบ Live Map ของ `voice-island.com`:

---

## 1. ข้อมูลพื้นฐานที่ได้จากการ Reverse-Engineering
* **Telemetry Data**: ดึงพิกัดผู้เล่นจาก `GET https://api.voice-island.com/api/players` (Polling ทุกๆ 2-5 วินาที)
* **Coordinate Conversion**:
  $$\text{svgX} = \frac{(x_{\text{game}} / 1000) - (-505)}{607 - (-505)} \times 731.25$$
  $$\text{svgY} = \frac{(y_{\text{game}} / 1000) - (-607)}{509 - (-607)} \times 732.75$$
  *(หมายเหตุ: $x_{\text{game}}$ และ $y_{\text{game}}$ มีการสลับแกนตามระบบพิกัด 2D Cartographic ของ The Isle)*
* **Map Asset**: `gateway-v0.21.7-base.webp` (731.25 x 732.75 px)

---

## 2. ขั้นตอนการพัฒนา (Phases)

### เฟสที่ 1: โครงสร้างโปรเจกต์และ Core Engines (Core & Math Engine)
1. **Trail & Movement Engine (`trailEngine.ts` / `movement.py`)**:
   - คำนวณเวกเตอร์ทิศทาง (ForwardArrow) ด้วย $\text{atan2}(\Delta y, \Delta x)$
   - Deadzone Filter: ล็อคมุมเดิมหากขยับ $< 2\text{ px}$ เพื่อป้องกันลูกศรสั่นตอนยืนนิ่ง
   - Ring Buffer Footprints: บันทึกประวัติรอยเท้าไม่เกิน 12 จุดพร้อม Alpha Decay
   - Stacking Optimization: ตรวจสอบระยะห่างจากจุดล่าสุด ถ้า $< 12\text{ px}$ จะไม่ปั๊มรอยเท้าใหม่
   - Proximity Footprint Culling: ซ่อนรอยเท้าของเพื่อนร่วมทีมหากอยู่ใกล้ตัวเรา $< 60\text{ px}$
2. **Pack Points (Marker Clustering Engine)**:
   - ตรวจจับระยะห่างของผู้เล่นในปาร์ตี้ หากใกล้กันเกินเกณฑ์ (เช่น $< 35\text{ px}$) ให้ยุบเป็น Cluster Icon `[N]`
   - ขยายตัวออกเป็นวงกลม (Radial/Spiderfy) เมื่อซูมใกล้หรือนำเมาส์ไปชี้ในโหมด Interactive

---

### เฟสที่ 2: ระบบนำทางและมาร์กจุด (Waypoints & Tracking System)
1. **Shared Waypoints**:
   - ระบบเพิ่ม/ลบจุดมาร์ก (Camp, Danger, Water, Food) พร้อมชื่อ สี และพิกัด
   - WebRTC / PeerJS DataChannel สำหรับกระจายจุดมาร์กและพิกัดให้เพื่อนในห้อง P2P
2. **Tracking Vector Line**:
   - ลากเส้นประ (Dotted Line) จากหมุดตัวเราไปยังเป้าหมายที่เลือก Track (Party Member หรือ Waypoint)
   - คำนวณระยะทางแบบ Real-time: $\text{Distance} = \sqrt{\Delta x^2 + \Delta y^2}$ และแสดงป้ายระยะทางกึ่งกลางเส้น
   - Off-screen Indicator: ลูกศรชี้ขอบจอพร้อมบอกระยะทางเมื่อเป้าหมายอยู่นอกกรอบ Minimap

---

### เฟสที่ 3: ระบบ UI Overlay และ Minimap HUD
1. **Minimap Component**:
   - ตำแหน่งลอยมุมบนซ้าย (Top-Left) ปรับแต่งได้ (กรอบสี่เหลี่ยม / เรดาร์วงกลม)
   - Player-centered Camera: ขยับแผนที่และหมุดตามตำแหน่งผู้เล่นอัตโนมัติ
   - แผงตั้งค่าขนาดเล็ก (Mini Config): ปรับ Opacity, ขนาด (Small/Med/Large), และ Zoom Level
2. **Interactive Map Mode**:
   - แผนที่ขยายเต็มจอ (Fullmap) เมื่อกดปุ่ม `M`
   - ค้นหา/เลือกสมาชิกหรือ Waypoint ผ่าน Drawer เมนู Track

---

### เฟสที่ 4: การรองรับระบบปฏิบัติการข้ามแพลตฟอร์ม (Cross-Platform: Windows 11 & Linux Cinnamon/X11)
1. **Window Transparency & Overlay**:
   - **Windows 11**: ใช้ Win32 API (`WS_EX_LAYERED`, `WS_EX_TRANSPARENT`) หรือ Tauri/PyQt สำหรับ Click-through
   - **Linux Cinnamon (X11)**:
     - ใช้ X11 Shape Extension (`XShapeCombineRectangles`) เพื่อเจาะ input mask ให้คลิกทะลุ 100% ในโหมด In-Game
     - ตั้งค่าหน้าต่างโปร่งแสงด้วย Compositor (`_NET_WM_WINDOW_TYPE_DOCK` หรือ `_NET_WM_STATE_ABOVE`)
     - รองรับการสลับ Input Mask กลับมาคลิกได้ทันทีเมื่อเปิดโหมด Interactive (`M`)
2. **Global Hotkeys**:
   - รองรับปุ่มลัดระดับระบบ (`M`, `F7`, `+`, `-`) ทั้งบน Windows (RegisterHotKey) และ Linux X11 (XGrabKey / pynput)
3. **Core Application Stacks**:
   - โค้ด Core Logic (Trail Engine, Math, Clustering, WebRTC) ออกแบบให้แยกจาก Window Layer อิสระ สามารถรันได้ทั้งบน Tauri (Rust + Webview) และ Python/PyQt5 / Web browser client

---

## 3. แผนการตรวจสอบและทดสอบ (Verification)
1. ตรวจสอบการแปลงพิกัดเทียบกับพิกัดจริงบน Live Map
2. ทดสอบ Syntax / Compile ของโค้ด
3. ทดสอบการลากเส้น Track Line และความแม่นยำของระยะทาง
