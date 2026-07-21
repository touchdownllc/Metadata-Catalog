# SYSTEM

You extract ONE factual claim about an Ed-Fi data element: does this row
describe conditional logic — i.e., a rule that says "IF <condition>
THEN <value or reporting obligation>"? You are NOT scoring anything.
You answer true/false for each element and quote the source text
verbatim for any True claim.

Rules:
- If has_conditional_logic is true, spans MUST contain >= 1 quote drawn
  verbatim from definition_text / business_rules_text /
  element_specific_rules — not paraphrased.
- If has_conditional_logic is false, spans MUST be [].
- Descriptor / Type / GradeLevel / Category suffix fields are simple
  value lookups, NOT conditional logic. Return false.
- Counting or aggregating records with a filter IS conditional logic
  (the filter is the condition).
- IF / WHEN / CASE / UNLESS in rules text is a strong positive signal.
- **Must reference the element:** the conditional clause must name the
  scored element (by name or unambiguous pronoun/phrase referring to
  it) AND describe a condition on the element's VALUE OR on whether
  the element is reported. A conditional that lives only in
  `element_specific_rules` is element-scoped by construction and
  qualifies. A conditional that appears only in entity-level
  `business_rules_text` and talks about the entity's submission window,
  population gate, or reporting obligation — without naming the
  element AND describing a value-level or per-element-reporting
  condition — is NOT element-level conditional logic. "Element X is
  reported for [population]" or "Report X during [submission window]"
  is an entity-level reporting gate, not element-level conditionality,
  even when X is named.
- **Pure value-range, format, or domain constraints are NOT conditional
  logic.** A statement that the element's value must fall within a
  range ("date range from July 1 through June 30", "must be in the
  range of 001-698", "values 0–100"), match a format ("must be a valid
  date", "alphanumeric"), or come from an allowed domain ("must be a
  positive integer") is a single-branch validity check on the element's
  own value, not branch logic that selects between distinct outcomes
  based on a separate condition. Return false. Conditional logic
  requires either (a) two or more value branches keyed on a condition
  ("if X then 'A' else 'B'"), (b) presence-vs-absence keyed on a
  condition ("if Y, must submit Z"), or (c) a derivation rule that
  inspects another field's value. Pure pointer / cross-reference rules
  ("follow the Resource Guide rules for IDs") are also not conditional
  logic — they delegate validity, not branch behavior.
- **Element-scoped grade-band / population / school-type conditionals
  ARE positive.** Read element_name AND definition_text together. When
  the field is explicitly scoped to a grade-band ("credits required in
  grades 7 or 8"), school-type ("if a student attends a Choice
  school"), or population subset ("for English Learners only"), the
  implicit IF on grade-band / school-type / population is branch logic
  on whether the element's value applies. This differs from
  entity-level reporting gates: the rule here speaks to what the
  ELEMENT means or when its VALUE is populated, not just when the
  entity is submitted. If the element's name encodes the population
  scope and definition_text confirms it, treat as conditional even if
  element_specific_rules is empty.
- If you cannot tell from the source text, return false with
  confidence="low". Do not guess.

Return a JSON array — no prose, no markdown fences — with one object
per input element, in input order, conforming to:

  [
    {"element_name": "<must match input exactly>",
     "has_conditional_logic": true | false,
     "spans": ["<verbatim quote from source text>", ...],
     "confidence": "high" | "medium" | "low"}
  ]

Examples:

  [positive] element_name=attendanceEventCategoryType,
             rules="Report 'Tardy' when student arrives >=15 min late"
             -> true; spans=["Report 'Tardy' when student arrives >=15 min late"]

  [positive] element_name=actualDaysAttendance,
             rules="Count days attended excluding non-instructional days"
             -> true; spans=["excluding non-instructional days"]
                (the exclusion is a filter condition)

  [negative] element_name=academicSubjectDescriptor,
             data_type=Descriptor,
             rules="Select from AcademicSubject descriptor table"
             -> false; spans=[]

  [negative] element_name=gradeLevelDescriptor,
             rules="(none)"
             -> false; spans=[]

  [negative] element_name=beginDate, data_type=date,
             rules="First day of enrollment"
             -> false; spans=[]

  [negative — VALUE-RANGE CONSTRAINT, not branch logic]
             element_name=DisciplineDate, data_type=date,
             element_specific_rules="DisciplineDate must be a valid
                 date. The date range for the DisciplineDate is from
                 July 1 through June 30 of each reporting year."
             -> false; spans=[]
                (single-branch validity check on the element's own
                value — no separate condition selecting between
                outcomes)

  [negative — FORMAT / DOMAIN constraint, not branch logic]
             element_name=SchoolId,
             element_specific_rules="The last three characters of
                 SchoolId must be in the range of 001-698. 699 is
                 designated for summer school."
             -> false; spans=[]
                (range + reserved-value note — both are validity rules
                on the element's own value, not branch logic)

  [negative — POINTER to external rule, not branch logic]
             element_name=Organization,
             element_specific_rules="Follow the Resource Guide rules
                 for reporting organizations/campus IDs."
             -> false; spans=[]
                (delegation to another rule source; no IF/THEN visible
                in the source text the model sees)

  [negative — ENTITY-LEVEL SUBMISSION RULE] element_name=SchoolYear,
             business_rules_text (entity-level): "This entity is reported
                 under PEIMS Submission 1 for grade 9-12 students. Report
                 once per student per school year."
             element_specific_rules="(none)"
             -> false; spans=[]
                (rule talks about the submission window + population,
                not about SchoolYear specifically — entity-level gate)

  [negative — POPULATION FILTER, element named but reporting-gate only]
             element_name=EndorsementPursuing,
             business_rules_text (entity-level): "EndorsementPursuing is
                 reported at the end of the school year for all
                 non-graduate students in grades 9-12 (PEIMS Submission
                 3 only)."
             -> false; spans=[]
                (element is named, but the rule is a reporting-window
                + population gate — describes WHO/WHEN to report, not
                a condition on the element's VALUE)

  [positive — ELEMENT NAMED in value-conditional] element_name=DiplomaType,
             business_rules_text (entity-level): "Report DiplomaType
                 values '04' or '05' only when GraduationYear >= 2026."
             -> true; spans=["Report DiplomaType values '04' or '05'
                             only when GraduationYear >= 2026."]
                (element named + value-level condition)

  [positive — GRADE-BAND-SCOPED ELEMENT, conditional in definition_text]
             element_name=nonHsHealthEducationCredits,
             definition_text="Health education credits required in
                 grades 7 or 8."
             element_specific_rules="(none)"
             -> true; spans=["Health education credits required in grades 7 or 8."]
                (element name + definition together encode that the
                value applies only for the grades 7-8 population —
                implicit IF on grade-band selecting whether this
                credit-count is reported. Element-scoped, not an
                entity submission window.)

  [positive — SCHOOL-TYPE-SCOPED ELEMENT, conditional in definition_text]
             element_name=privateSchoolChoiceProgramParticipant,
             definition_text="Indicator reported if a student attends
                 a Choice school under the Wisconsin Parental Choice
                 Program."
             element_specific_rules="(none)"
             -> true; spans=["Indicator reported if a student attends a Choice school under the Wisconsin Parental Choice Program."]
                (the value is populated only for the Choice-school
                population — IF on school-type selecting whether the
                indicator applies. Element-scoped value-presence.)

# USER

<entity_context>
  entity: CourseOffering
  domain: StudentAcademicRecord
  state: AZ
  business_rules_text (entity-level): [Rule 60000] If the reported value for School Year is not valid, report the discrepancy as an error. Invalid Value [School Year] (Course Section)
[Rule 60004] If the Course End Date is reported before the Course Begin Date, report the discrepancy as an error. [Course End Date] Before [Course Begin Date]
[Rule 60005] If the Course End Date is not in the current school year, report the discrepancy as an error. [Course Begin Date] not in Reported [School Year]
[Rule 60006] If the Course End Date is not in the current school year, report the discrepancy as an error. [Course End Date] not in [Reported School Year]
[Rule 60007] If the Course Section was reported with no students enrolled, report the discrepancy as a warning.
[Rule 60009] If a course is not a high school course with low grade of 9 or above, report the discrepancy as a warning.
[Rule 60010] If the value for a high school course is not 'Y' or 'N', report the discrepancy as an error
[Rule 60012] If the teacher/provider teacher's Begin Date is before the Course Begin Date, report the discrepancy as an error. The Teacher [Begin Date] is before the course starts
[Rule 60013] If the teacher/provider teacher's Begin Date is after the Course End Date, report the discrepancy as an error. The Teacher [Begin Date] is after the course has ended
[Rule 60014] If the teacher/provider teachers End Date is before the course start date, report the discrepancy as an error. The Teacher [End Date] is before the course has started
[Rule 60015] If the teacher end date is after the course end date, then report the discrepancy as an error. The Teacher [End Date] is after the course has ended
[Rule 60026] If a student is submitted with a Course-Exit-Date and no Exit Type, report the discrepancy as an error. Exit Type is required when Course-Exit-Date is provided.
[Rule 60027] If a student is submitted in a course as Dual Credit and the course is not identified as Dual Credit, report the discrepancy as an error. Student is shown as {1} which does not align with the [Dual Credit] availability
[Rule 60029] If the Student-Course-Entry-Date is before the Course-Begin-Date, report the discrepancy as an error. Student [Course Entry Date] Before [Course Begin Date]
[Rule 60030] If the Student-Course-Exit-Date is after the Course- End-Date, report the discrepancy as an error. Student [Course Exit Date] After [Course End Date]
[Rule 60031] If the student is enrolled simultaneously in two Sections of the same Course, report the discrepancy as a warning. Student is simultaneously enrolled in two sections of the same course.
[Rule 60033] If a high school student, taking a course with a low grade/high grade between 9-12, is submitted with an Exit Type and Exit Date for courses as Complete Full Credit (CF) or Complete Partial credit (CP) and credits earned is not reported, report the discrepancy as an error.
[Rule 60038] If the Educator ID is not submitted for the staff, instructing the CTE course section, report the discrepancy as a warning.
</entity_context>

<elements>
  - element_name: courseCode
    data_type: String
    definition_text: A unique alphanumeric code assigned to a course.
    element_specific_rules: (none)
  - element_name: educationOrganizationId
    data_type: Integer
    definition_text: The local code assigned by the LEA or Campus that identifies the organization of subject matter and related learning experiences provided for the instruction of students.
    element_specific_rules: (none)
  - element_name: endOfCourseAssessmentCodeDescriptor
    data_type: Descriptor
    definition_text: Submitted for courses that end with Assessment testing
    element_specific_rules: (none)
  - element_name: localCourseCode
    data_type: String
    definition_text: The local code assigned by the School that identifies the course offering provided for the instruction of students.
    element_specific_rules: (none)
  - element_name: localCourseTitle
    data_type: String
    definition_text: The descriptive name given to a course of study offered in the school, if different from the CourseTitle.
    element_specific_rules: (none)
  - element_name: schoolId
    data_type: Integer
    definition_text: School Identity Column: A unique alpha-numeric code assigned to a student by the state.
    element_specific_rules: (none)
  - element_name: schoolYear
    data_type: Integer
    definition_text: The identifier for the school year (e.g., 2010/11).
    element_specific_rules: (none)
  - element_name: sessionName
    data_type: String
    definition_text: The name of the session during the school year (e.g., 2016-2017 Fall Semester ).
    element_specific_rules: (none)
</elements>
