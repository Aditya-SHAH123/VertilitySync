"""
Curated, static ICD-10-CM reference for the diagnosis search box.

WHAT THIS IS
    A fixed lookup table of standard ICD-10-CM codes, weighted toward
    pulmonary/chest-imaging diagnoses given this application's focus,
    plus common general-medicine codes. A doctor searches it and picks a
    code to attach to a diagnosis they are recording - or skips it
    entirely and just types a diagnosis name, which is equally valid.

WHAT THIS IS NOT
    Not exhaustive (full ICD-10-CM has ~70,000 codes), not a diagnostic
    tool, and not AI-generated or AI-suggested. It never inspects a
    patient's data, an image, or a note to suggest a code - it only
    matches whatever text the doctor types against this fixed table. If a
    doctor's diagnosis isn't in here, they type it as free text instead;
    diagnosis_name (not the code) is the only required field on a
    diagnosis record (see api/patients.py).

MAINTENANCE
    Codes and descriptions below reflect standard ICD-10-CM as commonly
    published; verify against the current CMS/WHO release before treating
    this as authoritative for billing or coding compliance purposes.
"""

ICD10_CODES = [
    # Interstitial lung disease / pulmonary fibrosis - this app's primary focus
    ("J84.10", "Pulmonary fibrosis, unspecified"),
    ("J84.112", "Idiopathic pulmonary fibrosis"),
    ("J84.111", "Idiopathic interstitial pneumonia"),
    ("J84.17", "Other interstitial pulmonary diseases with fibrosis in diseases classified elsewhere"),
    ("J84.9", "Interstitial pulmonary disease, unspecified"),
    ("J84.89", "Other specified interstitial pulmonary diseases"),
    ("J84.02", "Desquamative interstitial pneumonia"),
    ("J84.01", "Alveolar proteinosis"),
    ("J82.83", "Chronic hypersensitivity pneumonitis"),
    ("J82.82", "Acute hypersensitivity pneumonitis"),
    ("J82.81", "Subacute hypersensitivity pneumonitis"),
    ("J60", "Coalworker's pneumoconiosis"),
    ("J61", "Pneumoconiosis due to asbestos and other mineral fibers"),
    ("J62.8", "Pneumoconiosis due to other dust containing silica"),
    ("J63.0", "Aluminosis (of lung)"),
    ("J67.0", "Farmer's lung"),
    ("M34.81", "Systemic sclerosis with lung involvement"),

    # Obstructive lung disease
    ("J44.9", "Chronic obstructive pulmonary disease, unspecified"),
    ("J44.0", "COPD with acute lower respiratory infection"),
    ("J44.1", "COPD with acute exacerbation"),
    ("J45.909", "Unspecified asthma, uncomplicated"),
    ("J45.20", "Mild intermittent asthma, uncomplicated"),
    ("J45.40", "Moderate persistent asthma, uncomplicated"),
    ("J45.50", "Severe persistent asthma, uncomplicated"),
    ("J43.9", "Emphysema, unspecified"),
    ("J47.9", "Bronchiectasis, uncomplicated"),
    ("J41.0", "Simple chronic bronchitis"),
    ("J42", "Unspecified chronic bronchitis"),

    # Pulmonary vascular
    ("I26.99", "Other pulmonary embolism without acute cor pulmonale"),
    ("I26.90", "Septic pulmonary embolism without acute cor pulmonale"),
    ("I27.0", "Primary pulmonary hypertension"),
    ("I27.20", "Pulmonary hypertension, unspecified"),
    ("I27.21", "Secondary pulmonary arterial hypertension"),
    ("I27.89", "Other specified pulmonary heart diseases"),

    # Infectious / inflammatory
    ("J18.9", "Pneumonia, unspecified organism"),
    ("J12.89", "Other viral pneumonia"),
    ("J15.9", "Unspecified bacterial pneumonia"),
    ("J09.X1", "Influenza due to identified novel influenza A virus with pneumonia"),
    ("A15.0", "Tuberculosis of lung"),
    ("B44.1", "Other pulmonary aspergillosis"),
    ("D86.0", "Sarcoidosis of lung"),
    ("D86.2", "Sarcoidosis of lung with sarcoidosis of lymph nodes"),
    ("J98.4", "Other disorders of lung"),
    ("J84.116", "Cryptogenic organizing pneumonia"),

    # Pleural
    ("J90", "Pleural effusion, not elsewhere classified"),
    ("J91.8", "Pleural effusion in other conditions classified elsewhere"),
    ("J93.9", "Pneumothorax, unspecified"),
    ("J94.1", "Fibrothorax"),
    ("J94.8", "Other specified pleural conditions"),

    # Neoplasm
    ("C34.90", "Malignant neoplasm of unspecified part of unspecified bronchus or lung"),
    ("C34.10", "Malignant neoplasm of upper lobe, bronchus or lung, unspecified side"),
    ("C78.00", "Secondary malignant neoplasm of unspecified lung"),
    ("D14.30", "Benign neoplasm of unspecified bronchus and lung"),
    ("R91.1", "Solitary pulmonary nodule"),
    ("R91.8", "Other nonspecific abnormal finding of lung field"),

    # Cardiac comorbidities commonly co-managed with pulmonary disease
    ("I50.9", "Heart failure, unspecified"),
    ("I50.22", "Chronic systolic (congestive) heart failure"),
    ("I50.32", "Chronic diastolic (congestive) heart failure"),
    ("I48.91", "Unspecified atrial fibrillation"),

    # General / symptoms
    ("R05.9", "Cough, unspecified"),
    ("R06.02", "Shortness of breath"),
    ("R06.00", "Dyspnea, unspecified"),
    ("R07.9", "Chest pain, unspecified"),
    ("R09.02", "Hypoxemia"),
    ("R94.2", "Abnormal results of pulmonary function studies"),
    ("Z87.891", "Personal history of nicotine dependence"),
    ("F17.210", "Nicotine dependence, cigarettes, uncomplicated"),
]


def search_icd10(query, limit=20):
    """Case-insensitive substring match against code or description.
    Empty/whitespace query returns nothing - this is a search box, not a
    browsable index of the full table."""
    query = (query or "").strip().lower()
    if not query:
        return []
    matches = [
        {"code": code, "description": desc}
        for code, desc in ICD10_CODES
        if query in code.lower() or query in desc.lower()
    ]
    return matches[:limit]
