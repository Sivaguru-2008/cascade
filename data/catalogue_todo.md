# Catalogue verification to-do

Every ATC code in `drug_catalogue.csv` was entered from the WHO ATC/DDD index as the author remembered it.
**A human must check every row against the current WHO ATC index (https://atcddd.fhi.no/atc_ddd_index/)
before any result is presented.** The rows below need extra care because the code depends on the
formulation or the substance has more than one ATC code.

| drug_id | drug | code used | why it needs checking |
|---|---|---|---|
| D026 | acetylsalicylic acid | B01AC06 | antiplatelet (low dose); the analgesic code is N02BA01. # VERIFY |
| D066 | amoxicillin and beta-lactamase inhibitor | J01CR02 | combination code. # VERIFY |
| D087 | levodopa and decarboxylase inhibitor | N04BA02 | combination code (levodopa alone is N04BA01). # VERIFY |
| D115 | codeine | R05DA04 | antitussive code; codeine combinations sit under N02AJ. # VERIFY |
| D121 | salbutamol | R03AC02 | inhaled; the systemic code is R03CC02. # VERIFY |
| D122 | budesonide | R03BA02 | inhaled; the nasal code is R01AD05 and the intestinal one A07EA06. # VERIFY |
| D125 | prednisolone | H02AB06 | systemic code. # VERIFY |
| D047 | butylscopolamine | A03BB01 | also called hyoscine butylbromide. # VERIFY |
| D064 | clonazepam | N03AE01 | listed as an antiepileptic in ATC, often used as an anxiolytic. # VERIFY |
| D127 | colecalciferol | A11CC05 | vitamin D (A11CC). # VERIFY |
| DNOVA | Novacor | C09XX99 | **Fictional on purpose.** C09XX is not a real ATC level 4. Level 3 is set to C09X (see DECISIONS.md). |

The typical daily doses (`typical_daily_dose_mg`) are illustrative values for synthetic data only, not
dosing guidance. Lactulose and macrogol doses are approximate gram amounts written in mg.

`is_marker_class` follows section 4.1 at class level. Two judgement calls: febuxostat is marked because it
shares class M04AA with allopurinol, and mirabegron because it shares G04BD with the urinary antimuscarinics.
