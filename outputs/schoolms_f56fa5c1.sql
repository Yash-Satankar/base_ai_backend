-- ============================================================
-- Project  : SchoolMS
-- Generated: 2026-05-25 11:53:03
-- Engine   : AI DB Schema Generator
-- Rules    : 109 production rules applied
-- ============================================================

SET SQL_MODE = "NO_AUTO_VALUE_ON_ZERO";
SET time_zone = "+00:00";
START TRANSACTION;

-- unique_id_header_all table
CREATE TABLE unique_id_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  table_name VARCHAR(100) NOT NULL,
  id_for VARCHAR(50) NOT NULL,
  prefix VARCHAR(20) NOT NULL,
  last_id VARCHAR(15) NOT NULL,
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_unique_id_header_all_table_name (table_name)
);

-- student_header_all table
CREATE TABLE student_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  student_id VARCHAR(20) NOT NULL,
  name VARCHAR(100) NOT NULL,
  email VARCHAR(100) NOT NULL,
  phone VARCHAR(20) NOT NULL,
  address VARCHAR(200) NOT NULL,
  status TINYINT NOT NULL COMMENT '1=active,2=inactive',
  created_by INT NOT NULL,
  updated_by INT NOT NULL,
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  current_wallet_bal DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  INDEX idx_student_header_all_student_id (student_id),
  INDEX idx_student_header_all_email (email),
  CONSTRAINT fk_student_header_all_created_by FOREIGN KEY (created_by) REFERENCES staff_header_all (id),
  CONSTRAINT fk_student_header_all_updated_by FOREIGN KEY (updated_by) REFERENCES staff_header_all (id)
);

-- attendance_transaction_all table
CREATE TABLE attendance_transaction_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  student_id INT NOT NULL,
  attendance_date DATE NOT NULL,
  attendance_status TINYINT NOT NULL COMMENT '1=present,2=absent',
  created_by INT NOT NULL,
  updated_by INT NOT NULL,
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_attendance_transaction_all_student_id (student_id),
  INDEX idx_attendance_transaction_all_attendance_date (attendance_date),
  CONSTRAINT fk_attendance_transaction_all_created_by FOREIGN KEY (created_by) REFERENCES staff_header_all (id),
  CONSTRAINT fk_attendance_transaction_all_updated_by FOREIGN KEY (updated_by) REFERENCES staff_header_all (id),
  CONSTRAINT fk_attendance_transaction_all_student_id FOREIGN KEY (student_id) REFERENCES student_header_all (id)
);

-- fee_header_all table
CREATE TABLE fee_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  fee_id VARCHAR(20) NOT NULL,
  fee_name VARCHAR(100) NOT NULL,
  fee_amount DECIMAL(10,2) NOT NULL,
  status TINYINT NOT NULL COMMENT '1=active,2=inactive',
  created_by INT NOT NULL,
  updated_by INT NOT NULL,
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_fee_header_all_fee_id (fee_id),
  INDEX idx_fee_header_all_fee_name (fee_name),
  CONSTRAINT fk_fee_header_all_created_by FOREIGN KEY (created_by) REFERENCES staff_header_all (id),
  CONSTRAINT fk_fee_header_all_updated_by FOREIGN KEY (updated_by) REFERENCES staff_header_all (id)
);

-- fee_transaction_all table
CREATE TABLE fee_transaction_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  student_id INT NOT NULL,
  fee_id INT NOT NULL,
  payment_date DATE NOT NULL,
  payment_amount DECIMAL(10,2) NOT NULL,
  cgst_amount DECIMAL(10,2) NOT NULL,
  sgst_amount DECIMAL(10,2) NOT NULL,
  closing_balance DECIMAL(12,2) NOT NULL,
  payment_method VARCHAR(50) NOT NULL,
  status TINYINT NOT NULL COMMENT '1=paid,2=unpaid',
  created_by INT NOT NULL,
  updated_by INT NOT NULL,
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_fee_transaction_all_student_id (student_id),
  INDEX idx_fee_transaction_all_fee_id (fee_id),
  INDEX idx_fee_transaction_all_payment_date (payment_date),
  CONSTRAINT fk_fee_transaction_all_created_by FOREIGN KEY (created_by) REFERENCES staff_header_all (id),
  CONSTRAINT fk_fee_transaction_all_updated_by FOREIGN KEY (updated_by) REFERENCES staff_header_all (id),
  CONSTRAINT fk_fee_transaction_all_student_id FOREIGN KEY (student_id) REFERENCES student_header_all (id),
  CONSTRAINT fk_fee_transaction_all_fee_id FOREIGN KEY (fee_id) REFERENCES fee_header_all (id)
);

-- gst_invoice_header_all table
CREATE TABLE gst_invoice_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  invoice_id VARCHAR(20) NOT NULL,
  invoice_date DATE NOT NULL,
  invoice_amount DECIMAL(10,2) NOT NULL,
  cgst_amount DECIMAL(10,2) NOT NULL,
  sgst_amount DECIMAL(10,2) NOT NULL,
  status TINYINT NOT NULL COMMENT '1=active,2=inactive',
  created_by INT NOT NULL,
  updated_by INT NOT NULL,
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_gst_invoice_header_all_invoice_id (invoice_id),
  INDEX idx_gst_invoice_header_all_invoice_date (invoice_date),
  CONSTRAINT fk_gst_invoice_header_all_created_by FOREIGN KEY (created_by) REFERENCES staff_header_all (id),
  CONSTRAINT fk_gst_invoice_header_all_updated_by FOREIGN KEY (updated_by) REFERENCES staff_header_all (id)
);

-- payment_mode_header_all table
CREATE TABLE payment_mode_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  payment_mode_id VARCHAR(20) NOT NULL,
  payment_mode_name VARCHAR(100) NOT NULL,
  status TINYINT NOT NULL COMMENT '1=active,2=inactive',
  created_by INT NOT NULL,
  updated_by INT NOT NULL,
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_payment_mode_header_all_payment_mode_id (payment_mode_id),
  INDEX idx_payment_mode_header_all_payment_mode_name (payment_mode_name),
  CONSTRAINT fk_payment_mode_header_all_created_by FOREIGN KEY (created_by) REFERENCES staff_header_all (id),
  CONSTRAINT fk_payment_mode_header_all_updated_by FOREIGN KEY (updated_by) REFERENCES staff_header_all (id)
);

-- payment_transaction_all table
CREATE TABLE payment_transaction_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  payment_mode_id INT NOT NULL,
  payment_date DATE NOT NULL,
  payment_amount DECIMAL(10,2) NOT NULL,
  cgst_amount DECIMAL(10,2) NOT NULL,
  sgst_amount DECIMAL(10,2) NOT NULL,
  closing_balance DECIMAL(12,2) NOT NULL,
  payment_method VARCHAR(50) NOT NULL,
  status TINYINT NOT NULL COMMENT '1=paid,2=unpaid',
  created_by INT NOT NULL,
  updated_by INT NOT NULL,
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_payment_transaction_all_payment_mode_id (payment_mode_id),
  INDEX idx_payment_transaction_all_payment_date (payment_date),
  CONSTRAINT fk_payment_transaction_all_created_by FOREIGN KEY (created_by) REFERENCES staff_header_all (id),
  CONSTRAINT fk_payment_transaction_all_updated_by FOREIGN KEY (updated_by) REFERENCES staff_header_all (id),
  CONSTRAINT fk_payment_transaction_all_payment_mode_id FOREIGN KEY (payment_mode_id) REFERENCES payment_mode_header_all (id)
);

-- staff_header_all table
CREATE TABLE staff_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  staff_id VARCHAR(20) NOT NULL,
  name VARCHAR(100) NOT NULL,
  email VARCHAR(100) NOT NULL,
  phone VARCHAR(20) NOT NULL,
  address VARCHAR(200) NOT NULL,
  status TINYINT NOT NULL COMMENT '1=active,2=inactive',
  created_by INT NOT NULL,
  updated_by INT NOT NULL,
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  current_wallet_bal DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  INDEX idx_staff_header_all_staff_id (staff_id),
  INDEX idx_staff_header_all_email (email),
  CONSTRAINT fk_staff_header_all_created_by FOREIGN KEY (created_by) REFERENCES staff_header_all (id),
  CONSTRAINT fk_staff_header_all_updated_by FOREIGN KEY (updated_by) REFERENCES staff_header_all (id)
);

-- salary_transaction_all table
CREATE TABLE salary_transaction_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  staff_id INT NOT NULL,
  salary_date DATE NOT NULL,
  salary_amount DECIMAL(10,2) NOT NULL,
  cgst_amount DECIMAL(10,2) NOT NULL,
  sgst_amount DECIMAL(10,2) NOT NULL,
  closing_balance DECIMAL(12,2) NOT NULL,
  payment_method VARCHAR(50) NOT NULL,
  status TINYINT NOT NULL COMMENT '1=paid,2=unpaid',
  created_by INT NOT NULL,
  updated_by INT NOT NULL,
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_salary_transaction_all_staff_id (staff_id),
  INDEX idx_salary_transaction_all_salary_date (salary_date),
  CONSTRAINT fk_salary_transaction_all_created_by FOREIGN KEY (created_by) REFERENCES staff_header_all (id),
  CONSTRAINT fk_salary_transaction_all_updated_by FOREIGN KEY (updated_by) REFERENCES staff_header_all (id),
  CONSTRAINT fk_salary_transaction_all_staff_id FOREIGN KEY (staff_id) REFERENCES staff_header_all (id)
);

This schema includes all the required tables for the School Management System, following the provided rules and fixing the issues mentioned. The `closing_balance` column has been added to the `fee_transaction_all`, `payment_transaction_all`, and `salary_transaction_all` tables to track the running balance. The schema also includes indexes and foreign key constraints to ensure data consistency and improve query performance.

COMMIT;
-- ============================================================
-- End of schema
-- ============================================================
