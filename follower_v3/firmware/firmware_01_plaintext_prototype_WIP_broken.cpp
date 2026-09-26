/*
 Repeated Peak-Finding Fabry-Perot Scan
 Arduino Due Version
*/
#include <math.h>

#define N 500              // Samples per scan
#define ANALOG_PIN A0
#define TRIGGER_PIN 53
#define HIGH_THRESH 800
#define LOW_THRESH 400
#define NUM_PEAKS 20
#define MAX_SCANS 100
#define NOISE_THRESH 100
#define CLUSTER_GAP 75
#define MAX_CLUSTERS 3

volatile uint16_t buffer[N];
volatile int adcIndex = 0;
volatile int delayus = 200;

// Storage for all found peaks
uint16_t highPeaks[NUM_PEAKS];
uint16_t lowPeaks[NUM_PEAKS];
uint16_t initialPeaks[NUM_PEAKS];

uint16_t highPeakPos[NUM_PEAKS];
uint16_t lowPeakPos[NUM_PEAKS];
uint16_t initialPeakPos[NUM_PEAKS];

int foundHigh = 0;
int foundLow = 0;
int foundInitial = 0;
int SEARCH_WINDOW = 50;

uint16_t currentHighVals[MAX_CLUSTERS];
uint16_t currentLowVals[MAX_CLUSTERS];
int currentHighPos[MAX_CLUSTERS];
int currentLowPos[MAX_CLUSTERS];
bool highPeakLost[MAX_CLUSTERS];
bool lowPeakLost[MAX_CLUSTERS];

float highClusterMeans[MAX_CLUSTERS];
float lowClusterMeans[MAX_CLUSTERS];

// ---------- Peak statistics structure ----------
struct PeakStats {
 float meanHeight;
 float stdHeight;
};

// ---------- Global stats ----------
PeakStats highStats;
PeakStats lowStats;

// ---------- Helper: check if peak lost ----------
bool isPeakLost(float maxVal, float meanHeight, float stdHeight) {
 if (maxVal < NOISE_THRESH) return true;
 if (maxVal < (meanHeight - 2.0 * stdHeight)) return true;
 return false;
}

// ---------- ADC setup ----------
void setupADC() {
 ADC->ADC_CR = ADC_CR_SWRST;
 ADC->ADC_MR |= ADC_MR_PRESCAL(14);
 ADC->ADC_CHER = ADC_CHER_CH7;
}

// ---------- Scan acquisition ----------
void acquireScan() {
 adcIndex = 0;
 while (adcIndex == 0) {
   if (!digitalRead(TRIGGER_PIN)) {
     while (!digitalRead(TRIGGER_PIN));
     delayMicroseconds(delayus);
     while (adcIndex < N) {
       ADC->ADC_CR = ADC_CR_START;
       while (!(ADC->ADC_ISR & ADC_ISR_EOC7));
       buffer[adcIndex++] = ADC->ADC_CDR[7];
     }
     return;
   }
 }
}

// ---------- Local max detection ----------
bool isLocalMax(int i, int window) {
 uint16_t val = buffer[i];
 for (int j = -window; j <= window; j++) {
   if (j == 0) continue;
   if (buffer[i + j] > val) return false;
 }
 return true;
}

// ---------- Peak clustering ----------
int clusterPeaks(uint16_t *positions, int count, float *clusterMeans) {
 if (count <= 0) return 0;
 for (int i = 1; i < count; i++) {
   uint16_t key = positions[i];
   int j = i - 1;
   while (j >= 0 && positions[j] > key) {
     positions[j + 1] = positions[j];
     j--;
   }
   positions[j + 1] = key;
 }
 int clusterCount = 0;
 uint32_t sum = positions[0];
 int n = 1;
 for (int i = 1; i < count; i++) {
   if ((positions[i] - positions[i - 1]) > CLUSTER_GAP) {
     clusterMeans[clusterCount++] = (float)sum / n;
     sum = positions[i];
     n = 1;
     if (clusterCount >= MAX_CLUSTERS) break;
   } else {
     sum += positions[i];
     n++;
   }
 }
 if (clusterCount < MAX_CLUSTERS) {
   clusterMeans[clusterCount++] = (float)sum / n;
 }
 return clusterCount;
}

// ---------- Peak stats ----------
PeakStats computePeakStats(uint16_t *heights, uint16_t *positions, int count) {
 PeakStats s = {0, 0};
 if (count <= 0) return s;
 float sumH = 0;
 for (int i = 0; i < count; i++) sumH += heights[i];
 s.meanHeight = sumH / count;
 float varH = 0;
 for (int i = 0; i < count; i++) varH += pow(heights[i] - s.meanHeight, 2);
 if (count > 1) s.stdHeight = sqrt(varH / (count - 1));
 return s;
}

// ---------- Add new peaks ----------
void addNewPeaks() {
 int window = 5;
 for (int i = window; i < N - window; i++) {
   uint16_t val = buffer[i];
   if (val > HIGH_THRESH && isLocalMax(i, window)) {
     if (foundHigh < NUM_PEAKS) {
       highPeaks[foundHigh] = val;
       highPeakPos[foundHigh++] = i;
     }
   } else if (val > LOW_THRESH && val < HIGH_THRESH && isLocalMax(i, window)) {
     if (foundLow < NUM_PEAKS) {
       lowPeaks[foundLow] = val;
       lowPeakPos[foundLow++] = i;
     }
   }
 }
}

// ---------- Initialization ----------
void initialize_peak_vals_locations() {
 foundHigh = 0;
 foundLow = 0;
 int scanCount = 0;

 while (scanCount < MAX_SCANS) {
   acquireScan();
   addNewPeaks();
   scanCount++;
 }

 highStats = computePeakStats(highPeaks, highPeakPos, foundHigh);
 lowStats  = computePeakStats(lowPeaks, lowPeakPos, foundLow);

 int nHighClusters = clusterPeaks(highPeakPos, foundHigh, highClusterMeans);
 int nLowClusters  = clusterPeaks(lowPeakPos,  foundLow,  lowClusterMeans);

 SerialUSB.print("Scans performed: "); SerialUSB.println(scanCount);
 SerialUSB.print("SEARCH_WINDOW=");

 if (nHighClusters > 0 && nLowClusters > 0) {
   int diff = abs((int)highClusterMeans[0] - (int)lowClusterMeans[0]);
   SEARCH_WINDOW = max(5, diff / 2.5);
 }

 SerialUSB.println(SEARCH_WINDOW);
}

// ---------- Active monitoring ----------
void trackPeaksInRegions() {
 acquireScan();

 // --- Track high peaks ---
 for (int i = 0; i < MAX_CLUSTERS; i++) {
   if (highClusterMeans[i] <= 0) continue;
   int center = (int)highClusterMeans[i];
   int start = max(0, center - SEARCH_WINDOW);
   int end   = min(N - 1, center + SEARCH_WINDOW);

   uint16_t maxVal = 0;
   int maxPos = center;
   for (int j = start; j <= end; j++) {
     if (buffer[j] > maxVal) {
       maxVal = buffer[j];
       maxPos = j;
     }
   }
   currentHighVals[i] = maxVal;
   currentHighPos[i]  = maxPos;
   highPeakLost[i]    = isPeakLost(maxVal, highStats.meanHeight, highStats.stdHeight);
 }

 // --- Track low peaks ---
 for (int i = 0; i < MAX_CLUSTERS; i++) {
   if (lowClusterMeans[i] <= 0) continue;
   int center = (int)lowClusterMeans[i];
   int start = max(0, center - SEARCH_WINDOW);
   int end   = min(N - 1, center + SEARCH_WINDOW);

   uint16_t maxVal = 0;
   int maxPos = center;
   for (int j = start; j <= end; j++) {
     if (buffer[j] > maxVal) {
       maxVal = buffer[j];
       maxPos = j;
     }
   }
   currentLowVals[i] = maxVal;
   currentLowPos[i]  = maxPos;
   lowPeakLost[i]    = isPeakLost(maxVal, lowStats.meanHeight, lowStats.stdHeight);
 }
}

// ---------- Status printing ----------
void printPeakStatus() {
 SerialUSB.println("Active peak tracking:");
 for (int i = 0; i < MAX_CLUSTERS; i++) {
   if (highClusterMeans[i] > 0) {
     SerialUSB.print("High "); SerialUSB.print(i);
     SerialUSB.print(" pos="); SerialUSB.print(currentHighPos[i]);
     SerialUSB.print(" val="); SerialUSB.print(currentHighVals[i]);
     SerialUSB.print(" status=");
     SerialUSB.println(highPeakLost[i] ? "LOST" : "OK");
   }
 }
 for (int i = 0; i < MAX_CLUSTERS; i++) {
   if (lowClusterMeans[i] > 0) {
     SerialUSB.print("Low "); SerialUSB.print(i);
     SerialUSB.print(" pos="); SerialUSB.print(currentLowPos[i]);
     SerialUSB.print(" val="); SerialUSB.print(currentLowVals[i]);
     SerialUSB.print(" status=");
     SerialUSB.println(lowPeakLost[i] ? "LOST" : "OK");
   }
 }
}

// ---------- Setup and loop ----------
void setup() {
 SerialUSB.begin(250000);
 Serial.begin(9600);
 pinMode(ANALOG_PIN, INPUT);
 pinMode(TRIGGER_PIN, INPUT);
 setupADC();
 Serial.println("Arduino Due initialized and ready.");
}

void loop() {
 String command = SerialUSB.readStringUntil('\n');
 command.trim();

 if (command.startsWith("D")) {
   delayus = command.substring(1).toInt();
   SerialUSB.print("delayus set to: ");
   SerialUSB.println(delayus);
 }

 if (command == "R") {
   initialize_peak_vals_locations();
 }
}

void loop() {
 String command = SerialUSB.readStringUntil('\n');
 command.trim();

 if (command.startsWith("D")) {
   delayus = command.substring(1).toInt();
   SerialUSB.print("delayus set to: ");
   SerialUSB.println(delayus);
 }

 if (command == "R") {
   initialize_peak_vals_locations();
 }

 // Always run active monitoring
 if (highClusterMeans[0] > 0) {
   trackPeaksInRegions();
   printPeakStatus();
 }
}





// Old working code below, before active monitoring
///*
//  Repeated Peak-Finding Fabry-Perot Scan
//  Arduino Due Version
//*/
//#include <math.h>
//
//#define N 500              // Samples per scan
//#define ANALOG_PIN A0
//#define TRIGGER_PIN 53
//#define HIGH_THRESH 800
//#define LOW_THRESH 400
//#define NUM_PEAKS 20
//#define MAX_SCANS 100      // Maximum number of scans to try
//#define NOISE_THRESH 100
//#define CLUSTER_GAP 75   // Distance threshold (samples) to separate clusters
//#define MAX_CLUSTERS 3  // Max expected number of distinct resonances
//
//volatile uint16_t buffer[N];
//volatile int adcIndex = 0;
//volatile int delayus = 200;
//
//// Storage for all found peaks
//uint16_t highPeaks[NUM_PEAKS];
//uint16_t lowPeaks[NUM_PEAKS];
//uint16_t initialPeaks[NUM_PEAKS];
//
//uint16_t highPeakPos[NUM_PEAKS];
//uint16_t lowPeakPos[NUM_PEAKS];
//uint16_t initialPeakPos[NUM_PEAKS];
//
//int foundHigh = 0;
//int foundLow = 0;
//int foundInitial = 0;
//int SEARCH_WINDOW = 50;
//
//uint16_t currentHighVals[MAX_CLUSTERS];
//uint16_t currentLowVals[MAX_CLUSTERS];
//int currentHighPos[MAX_CLUSTERS];
//int currentLowPos[MAX_CLUSTERS];
//bool highPeakFound[MAX_CLUSTERS];
//bool lowPeakFound[MAX_CLUSTERS];
//
//// Output storage
//float highClusterMeans[MAX_CLUSTERS];
//float lowClusterMeans[MAX_CLUSTERS];
//
//struct PeakStats {
//  float meanHeight;
//  float stdHeight;
//};
//
//int clusterPeaks(uint16_t *positions, int count, float *clusterMeans) {
//  if (count <= 0) return 0;
//
//  // --- Sort positions (simple insertion sort for small N) ---
//  for (int i = 1; i < count; i++) {
//    uint16_t key = positions[i];
//    int j = i - 1;
//    while (j >= 0 && positions[j] > key) {
//      positions[j + 1] = positions[j];
//      j--;
//    }
//    positions[j + 1] = key;
//  }
//
//  // --- Cluster and compute means ---
//  int clusterCount = 0;
//  uint32_t sum = positions[0];
//  int n = 1;
//
//  for (int i = 1; i < count; i++) {
//    if ((positions[i] - positions[i - 1]) > CLUSTER_GAP) {
//      // close current cluster
//      clusterMeans[clusterCount++] = (float)sum / n;
//      sum = positions[i];
//      n = 1;
//
//      if (clusterCount >= MAX_CLUSTERS) break;
//    } else {
//      sum += positions[i];
//      n++;
//    }
//  }
//
//  // finalize last cluster
//  if (clusterCount < MAX_CLUSTERS) {
//    clusterMeans[clusterCount++] = (float)sum / n;
//  }
//
//  return clusterCount;
//}
//
//// Generic function to compute mean and std dev for heights and positions
//PeakStats computePeakStats(uint16_t *heights, uint16_t *positions, int count) {
//  PeakStats s = {0, 0};
//  if (count <= 0) return s;
//
//  // --- Compute means ---
//  float sumH = 0, sumP = 0;
//  for (int i = 0; i < count; i++) {
//    sumH += (float)heights[i];
//  }
//  s.meanHeight = sumH / count;
//
//  // --- Compute standard deviations ---
//  float varH = 0, varP = 0;
//  for (int i = 0; i < count; i++) {
//    varH += pow((float)heights[i] - s.meanHeight, 2);
//  }
//  if (count > 1) {
//    s.stdHeight = sqrt(varH / (count - 1));
//  }
//
//  return s;
//}
//
//void setup() {
//  SerialUSB.begin(250000);
//  Serial.begin(9600);
//  pinMode(ANALOG_PIN, INPUT);
//  pinMode(TRIGGER_PIN, INPUT);
//  setupADC();
//  Serial.println("Arduino Due initialized and ready.");
//  // initialize_peak_vals_locations();
//}
//
//void loop() {
//  String command = SerialUSB.readStringUntil('\n');
//  command.trim();
//
//  if (command.startsWith("D")) {
//    delayus = command.substring(1).toInt();
//    SerialUSB.print("delayus set to: ");
//    SerialUSB.println(delayus);
//  }
//
//  if (command == "R") {
//    initialize_peak_vals_locations();
//  }
//}
//
///* -------------------- ADC Setup -------------------- */
//void setupADC() {
//  ADC->ADC_CR = ADC_CR_SWRST;          // Reset ADC
//  ADC->ADC_MR |= ADC_MR_PRESCAL(14);   // Set prescaler
//  ADC->ADC_CHER = ADC_CHER_CH7;        // Enable A0
//}
//
///* -------------------- Acquire ADC Scan -------------------- */
//void acquireScan() {
//  adcIndex = 0;
//  while (adcIndex == 0) {
//    if (!digitalRead(TRIGGER_PIN)) { // if trigger low
//        while (!digitalRead(TRIGGER_PIN)); // wait until trigger high
//        delayMicroseconds(delayus);
//        while (adcIndex < N) {
//          ADC->ADC_CR = ADC_CR_START;
//          while (!(ADC->ADC_ISR & ADC_ISR_EOC7));
//          buffer[adcIndex++] = ADC->ADC_CDR[7];
//        }
//        return;
//    }
//    // else trigger is high and we wait for trigger to go low
//  }
//}
//
///* -------------------- Add New Peaks -------------------- */
//void addNewPeaks0(){
//  int window = 5;
//  for (int i = window; i < N - window; i++) {
//    uint16_t val = buffer[i];
//
//    // High peaks
//    if (isLocalMax(i, window) && (val > NOISE_THRESH)) {
//       initialPeaks[foundInitial] = val;
//       initialPeakPos[foundInitial] = i;
//       foundInitial++;
//    }
//
//    if (foundInitial >= NUM_PEAKS) return;
//  }
//}
//
//void addNewPeaks() {
//  int window = 5;
//  for (int i = window; i < N - window; i++) {
//    uint16_t val = buffer[i];
//
//    // High peaks
//    if (val > HIGH_THRESH && isLocalMax(i, window)) {
//      if (foundHigh < NUM_PEAKS) {
//        highPeaks[foundHigh] = val;
//        highPeakPos[foundHigh] = i;
//        foundHigh++;
//      }
//    }
//
//    // Low peaks
//    else if (val > LOW_THRESH && val < HIGH_THRESH && isLocalMax(i, window)) {
//      if (foundLow < NUM_PEAKS) {
//        lowPeaks[foundLow] = val;
//        lowPeakPos[foundLow] = i;
//        foundLow++;
//      }
//    }
//
//    // if (foundHigh >= NUM_PEAKS && foundLow >= NUM_PEAKS) return;
//  }
//}
//
///* -------------------- Check Local Max -------------------- */
//bool isLocalMax(int i, int window) {
//  uint16_t val = buffer[i];
//  for (int j = -window; j <= window; j++) {
//    if (j == 0) continue;
//    if (buffer[i + j] > val) return false;
//  }
//  return true;
//}
//
///* -------------------- Send Results -------------------- */
//void sendResults() {
//  SerialUSB.println("BEGIN_RESULTS");
//
//  // Send peak info
//  for (int i = 0; i < NUM_PEAKS; i++) {
//    if (highPeaks[i]>0){
//      SerialUSB.print("High "); SerialUSB.print(i);
//      SerialUSB.print(": val="); SerialUSB.print(highPeaks[i]);
//      SerialUSB.print(" pos="); SerialUSB.println(highPeakPos[i]);
//    }
//  }
//  for (int i = 0; i < NUM_PEAKS; i++) {
//    if (lowPeaks[i] > 0) {
//      SerialUSB.print("Low "); SerialUSB.print(i);
//      SerialUSB.print(": val="); SerialUSB.print(lowPeaks[i]);
//      SerialUSB.print(" pos="); SerialUSB.println(lowPeakPos[i]);
//    }
//  }
//
//  SerialUSB.println("END_RESULTS");
//
//  // Optionally: send last buffer
//  SerialUSB.println("BEGIN_SCAN");
//  SerialUSB.write((uint8_t*)buffer, N * sizeof(uint16_t));
//  SerialUSB.println("END_SCAN");
//  SerialUSB.flush();
//}
//
//void sendStats(PeakStats highStats, PeakStats lowStats){
//  SerialUSB.println("Start_High_Peak_Stats");
//  SerialUSB.print("meanHeight=");
//  SerialUSB.print(highStats.meanHeight, 2);
//  SerialUSB.print(" stdHeight=");
//  SerialUSB.print(highStats.stdHeight, 2);
//  SerialUSB.println();
//  SerialUSB.println("End_High_Peak_Stats");
//  SerialUSB.println("Start_Low_Peak_Stats");
//  SerialUSB.print("meanHeight=");
//  SerialUSB.print(lowStats.meanHeight, 2);
//  SerialUSB.print(" stdHeight=");
//  SerialUSB.print(lowStats.stdHeight, 2);
//  SerialUSB.println();
//  SerialUSB.println("End_Low_Peak_Stats");
//}
//
//void sendClusters(int nHighClusters, int nLowClusters) {
//  SerialUSB.println("Start_High_Clusters");
//  SerialUSB.print("High clusters found: ");
//  SerialUSB.println(nHighClusters);
//  for (int i = 0; i < nHighClusters; i++) {
//    SerialUSB.print("High cluster "); SerialUSB.print(i);
//    SerialUSB.print(" meanPos = ");
//    SerialUSB.println(highClusterMeans[i], 2);
//  }
//
//  SerialUSB.println("Start_Low_Clusters");
//  SerialUSB.print("Low clusters found: ");
//  SerialUSB.println(nLowClusters);
//  for (int i = 0; i < nLowClusters; i++) {
//    SerialUSB.print("Low cluster "); SerialUSB.print(i);
//    SerialUSB.print(" meanPos = ");
//    SerialUSB.println(lowClusterMeans[i], 2);
//  }
//  SerialUSB.println("End_Clusters");
//}
//
//
//void initialize_peak_vals_locations(){
//  foundHigh = 0;
//  foundLow = 0;
//  int scanCount = 0;
//
//  while (scanCount < MAX_SCANS) {
//    acquireScan();
//    addNewPeaks();   // process and accumulate peaks
//    scanCount++;
//  }
//
//  PeakStats highStats = computePeakStats(highPeaks, highPeakPos, NUM_PEAKS);
//  PeakStats lowStats = computePeakStats(lowPeaks, lowPeakPos, NUM_PEAKS);
//
//  int nHighClusters = clusterPeaks(highPeakPos, foundHigh, highClusterMeans);
//  int nLowClusters  = clusterPeaks(lowPeakPos,  foundLow,  lowClusterMeans);
//
//  SerialUSB.print("Scans performed: ");
//  SerialUSB.println(scanCount);
//  sendStats(highStats, lowStats);
//  sendClusters(nHighClusters, nLowClusters);
//  sendResults();
//
//    // --- Dynamically set SEARCH_WINDOW ---
//  if (nHighClusters > 0 && nLowClusters > 0) {
//    int diff = abs((int)highClusterMeans[0] - (int)lowClusterMeans[0]);
//    SEARCH_WINDOW = max(5, diff / 2.5);   // set 1/2.5 of the spacing, minimum 5
//    SerialUSB.print("SEARCH_WINDOW set to ");
//    SerialUSB.println(SEARCH_WINDOW);
//  }
//
//}
//
////void trackPeaksInRegions() {
////  acquireScan();  // Fill buffer[N]
////
////  // --- Track high peaks ---
////  for (int i = 0; i < MAX_CLUSTERS; i++) {
////    if (highClusterMeans[i] <= 0) continue;  // skip uninitialized clusters
////
////    int center = (int)highClusterMeans[i];
////    int start = max(0, center - SEARCH_WINDOW);
////    int end   = min(N - 1, center + SEARCH_WINDOW);
////
////    uint16_t maxVal = 0;
////    int maxPos = center;
////    for (int j = start; j <= end; j++) {
////      if (buffer[j] > maxVal) {
////        maxVal = buffer[j];
////        maxPos = j;
////      }
////    }
////
////    bool lost = isPeakLost(maxVal, highStats.meanHeight, highStats.stdHeight);
////
////    currentHighVals[i] = maxVal;
////    currentHighPos[i] = maxPos;
////    highPeakFound[i] = lost;
////  }
////
////  // --- Track low peaks ---
////  for (int i = 0; i < MAX_CLUSTERS; i++) {
////    if (lowClusterMeans[i] <= 0) continue;
////
////    int center = (int)lowClusterMeans[i];
////    int start = max(0, center - SEARCH_WINDOW);
////    int end   = min(N - 1, center + SEARCH_WINDOW);
////
////    uint16_t maxVal = 0;
////    int maxPos = center;
////    for (int j = start; j <= end; j++) {
////      if (buffer[j] > maxVal) {
////        maxVal = buffer[j];
////        maxPos = j;
////      }
////    }
////
////    currentLowVals[i] = maxVal;
////    currentLowPos[i] = maxPos;
////    lowPeakFound[i] = (maxVal > NOISE_THRESH);
////  }
////}
//
//void printPeakStatus() {
//  SerialUSB.println("Active peak tracking:");
//  for (int i = 0; i < MAX_CLUSTERS; i++) {
//    if (highClusterMeans[i] > 0) {
//      SerialUSB.print("High "); SerialUSB.print(i);
//      SerialUSB.print(" pos="); SerialUSB.print(currentHighPos[i]);
//      SerialUSB.print(" val="); SerialUSB.print(currentHighVals[i]);
//      SerialUSB.print(" found="); SerialUSB.println(highPeakFound[i]);
//    }
//  }
//  for (int i = 0; i < MAX_CLUSTERS; i++) {
//    if (lowClusterMeans[i] > 0) {
//      SerialUSB.print("Low "); SerialUSB.print(i);
//      SerialUSB.print(" pos="); SerialUSB.print(currentLowPos[i]);
//      SerialUSB.print(" val="); SerialUSB.print(currentLowVals[i]);
//      SerialUSB.print(" found="); SerialUSB.println(lowPeakFound[i]);
//    }
//  }
//}
//
//bool isPeakLost(float maxVal, float meanHeight, float stdHeight) {
//  if (maxVal < NOISE_THRESH) return true;
//  if (maxVal < (meanHeight - 2.0 * stdHeight)) return true;
//  return false;
//}